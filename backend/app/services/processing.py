"""Processing service: validates and normalizes records of one dataset.

Idempotency: before writing new output for a record, its current normalized
row is marked superseded (is_current=False) and its open quarantine row is
marked resolved or superseded. A partial unique index guarantees at most one
current normalized row per source record, so retries never duplicate output.
"""
from collections import Counter

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.engines.normalization import ResolvedMapping, normalize_record, summarize_errors
from app.engines.validation import duplicate_status
from app.models import (
    Dataset,
    FieldMapping,
    NormalizedRecord,
    ProcessingRun,
    QuarantinedRecord,
    SourceRecord,
    TransformationLog,
    utcnow,
)
from app.services.canonical import get_canonical
from app.services.mappings import active_mappings, dataset_fields


class ProcessingError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def resolve_dataset_mappings(db: Session, dataset: Dataset, canonical: dict) -> tuple[dict, dict, list[dict], dict]:
    """Return ({target: ResolvedMapping}, {target: schema_error}, schema_issues,
    {target: [source fields whose suggestion awaits review]})."""
    fields = set(dataset_fields(dataset))
    active = active_mappings(db, dataset.source_id)
    by_target: dict[str, list[FieldMapping]] = {}
    for m in active.values():
        if m.mapping_status == "approved" and m.target_field and m.source_field in fields:
            by_target.setdefault(m.target_field, []).append(m)

    resolved: dict[str, ResolvedMapping] = {}
    schema_errors: dict[str, dict] = {}
    issues: list[dict] = []
    pending: dict[str, list[str]] = {}
    for fdef in canonical["fields"]:
        target = fdef["name"]
        group = by_target.get(target, [])
        if len(group) > 1:
            names = ", ".join(sorted(m.source_field for m in group))
            err = {"code": "CONFLICTING_MAPPING", "field": target, "value": None, "level": "schema",
                   "message": f"Several approved fields ({names}) map to {target}."}
            schema_errors[target] = err
            issues.append({**err, "severity": "breaking"})
            continue
        if group:
            m = group[0]
            resolved[target] = ResolvedMapping(m.id, m.version, m.source_field, target, m.mapping_method)
            continue
        waiting = [m for m in active.values()
                   if m.target_field == target and m.mapping_status == "suggested" and m.source_field in fields]
        if waiting:
            pending[target] = [m.source_field for m in waiting]
        listed = ", ".join(f"{m.source_field} (mapping {m.id}, confidence {m.confidence})" for m in waiting)
        if fdef.get("required"):
            hint = f" Pending suggestion: {listed} needs approval." if waiting else ""
            err = {"code": "UNMAPPED_REQUIRED_FIELD", "field": target, "value": None, "level": "schema",
                   "message": f"No approved mapping provides required field {target}.{hint}"}
            schema_errors[target] = err
            issues.append({**err, "severity": "breaking", "pending_mapping_ids": [m.id for m in waiting]})
        elif waiting:
            issues.append({
                "code": "MAPPING_PENDING_REVIEW", "field": target, "level": "schema", "severity": "warning",
                "message": f"{listed} -> {target} awaits review. Records with a value there are quarantined "
                           f"until the mapping is approved or rejected.",
                "pending_mapping_ids": [m.id for m in waiting],
            })
    return resolved, schema_errors, issues, pending


def refresh_dataset_status(db: Session, dataset: Dataset) -> None:
    open_q = db.scalar(
        select(QuarantinedRecord.id).where(QuarantinedRecord.dataset_id == dataset.id, QuarantinedRecord.status == "open")
    )
    dataset.processing_status = "processed_with_quarantine" if open_q else "processed"


def process_dataset(
    db: Session,
    dataset_id: int,
    record_ids: list[int] | None = None,
    trigger: str = "manual",
    recovery_operation_id: int | None = None,
) -> ProcessingRun:
    """Run validation + normalization for all records (or a subset). The caller commits."""
    dataset = db.get(Dataset, dataset_id)
    if not dataset:
        raise ProcessingError(f"Dataset {dataset_id} not found.", status_code=404)
    canonical = get_canonical(db).schema_definition
    policies = canonical.get("policies", {})
    dup_key = policies.get("duplicate_key")

    run = ProcessingRun(dataset_id=dataset.id, status="processing", trigger=trigger,
                        recovery_operation_id=recovery_operation_id)
    db.add(run)
    dataset.processing_status = "processing"
    db.flush()

    query = select(SourceRecord).where(SourceRecord.dataset_id == dataset.id).order_by(SourceRecord.record_index)
    if record_ids is not None:
        query = query.where(SourceRecord.id.in_(record_ids))
    records = db.scalars(query).all()
    scope_ids = {r.id for r in records}

    resolved, schema_errors, issues, pending = resolve_dataset_mappings(db, dataset, canonical)

    # Duplicate detection is per dataset: seed with current output outside this run's scope.
    seen: dict = {}
    for nr, idx in db.execute(
        select(NormalizedRecord, SourceRecord.record_index)
        .join(SourceRecord, SourceRecord.id == NormalizedRecord.source_record_id)
        .where(NormalizedRecord.dataset_id == dataset.id, NormalizedRecord.is_current.is_(True))
    ).all():
        if nr.source_record_id not in scope_ids and dup_key:
            seen.setdefault(nr.normalized_payload.get(dup_key), {"payload": nr.normalized_payload, "record_index": idx})

    outcomes = []
    for rec in records:
        outcome = normalize_record(rec.original_payload, canonical, resolved, schema_errors, pending)
        if outcome.ok and dup_key:
            key = outcome.normalized.get(dup_key)
            status, first = duplicate_status(key, outcome.normalized, seen)
            if status:
                what = "an identical record" if status == "DUPLICATE_RECORD" else "a record with different values"
                outcome.errors.append({
                    "code": status, "field": dup_key, "value": str(key), "level": "record",
                    "message": f"{dup_key} '{key}' already appears in {what} (row {first['record_index'] + 1}).",
                })
                outcome.normalized = None
                outcome.lineage = []
            else:
                seen[key] = {"payload": outcome.normalized, "record_index": rec.record_index}
        outcomes.append((rec, outcome))

    # Supersede previous outputs for records in scope (keeps history, avoids duplicates).
    previous_open = {
        q.source_record_id: q
        for q in db.scalars(
            select(QuarantinedRecord).where(
                QuarantinedRecord.source_record_id.in_(scope_ids), QuarantinedRecord.status == "open"
            )
        ).all()
    }
    superseded_outputs = db.execute(
        update(NormalizedRecord)
        .where(NormalizedRecord.source_record_id.in_(scope_ids), NormalizedRecord.is_current.is_(True))
        .values(is_current=False, superseded_by_run_id=run.id)
    ).rowcount
    db.flush()

    error_counts: Counter = Counter()
    resolved_from_quarantine = requarantined = 0
    now = utcnow()
    for rec, outcome in outcomes:
        prior = previous_open.get(rec.id)
        if outcome.ok:
            nr = NormalizedRecord(processing_run_id=run.id, source_record_id=rec.id, dataset_id=dataset.id,
                                  normalized_payload=outcome.normalized, validation_status="valid", is_current=True)
            db.add(nr)
            db.flush()
            db.add_all(
                TransformationLog(processing_run_id=run.id, source_record_id=rec.id, normalized_record_id=nr.id,
                                  validation_result="passed", **entry)
                for entry in outcome.lineage
            )
            run.records_valid += 1
            if prior:
                prior.status = "resolved"
                prior.resolved_at = now
                prior.resolution_run_id = run.id
                prior.resolution_note = f"Record passed validation in run {run.id} ({trigger})."
                resolved_from_quarantine += 1
        else:
            for e in outcome.errors:
                error_counts[e["code"]] += 1
            db.add(QuarantinedRecord(
                processing_run_id=run.id, source_record_id=rec.id, dataset_id=dataset.id,
                failure_reason=summarize_errors(outcome.errors), errors=outcome.errors,
                original_payload=rec.original_payload, status="open",
            ))
            run.records_quarantined += 1
            if prior:
                prior.status = "superseded"
                prior.resolution_run_id = run.id
                prior.resolution_note = f"Still failing in run {run.id}; see the newer quarantine entry."
                requarantined += 1
        run.records_processed += 1

    run.status = "completed_with_quarantine" if run.records_quarantined else "completed"
    run.completed_at = utcnow()
    run.summary = {
        "scope": "subset" if record_ids is not None else "full_dataset",
        "schema_issues": issues,
        "error_counts": dict(sorted(error_counts.items())),
        "mappings_used": [
            {"source_field": m.source_field, "target_field": t, "mapping_id": m.mapping_id,
             "mapping_version": m.version, "method": m.method}
            for t, m in resolved.items()
        ],
        "resolved_from_quarantine": resolved_from_quarantine,
        "requarantined": requarantined,
        "superseded_outputs": superseded_outputs,
    }
    db.flush()
    refresh_dataset_status(db, dataset)
    return run


def record_failed_run(db: Session, dataset_id: int, trigger: str, message: str) -> ProcessingRun:
    run = ProcessingRun(dataset_id=dataset_id, status="failed", trigger=trigger, completed_at=utcnow(),
                        summary={"error": message})
    db.add(run)
    dataset = db.get(Dataset, dataset_id)
    if dataset:
        dataset.processing_status = "failed"
    db.flush()
    return run
