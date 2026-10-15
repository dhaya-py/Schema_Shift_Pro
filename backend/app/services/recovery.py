"""Quarantine and recovery: identify affected records after a mapping change,
reprocess them for real, and record the recovery operation."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Dataset,
    NormalizedRecord,
    QuarantinedRecord,
    RecoveryOperation,
    TransformationLog,
)
from app.services import mappings as mapping_service
from app.services.mappings import dataset_fields
from app.services.processing import ProcessingError, process_dataset


def _open_quarantine_ids(db: Session, dataset_id: int) -> set[int]:
    return set(db.scalars(
        select(QuarantinedRecord.source_record_id).where(
            QuarantinedRecord.dataset_id == dataset_id, QuarantinedRecord.status == "open"
        )
    ).all())


def affected_records(db: Session, source_id: int, fields: list[str], targets: set[str],
                     superseded_mapping_ids: list[int]) -> dict[int, list[int]]:
    """{dataset_id: [source_record_id]} affected by a mapping change on `fields`.

    A record is affected when it is in open quarantine, or its current
    normalized output used a superseded mapping, or its output left one of
    the affected targets null because no mapping existed.
    """
    result: dict[int, list[int]] = {}
    datasets = db.scalars(select(Dataset).where(Dataset.source_id == source_id).order_by(Dataset.id)).all()
    for ds in datasets:
        if not set(fields) & set(dataset_fields(ds)):
            continue
        ids = _open_quarantine_ids(db, ds.id)
        stale = db.scalars(
            select(TransformationLog.source_record_id)
            .join(NormalizedRecord, NormalizedRecord.id == TransformationLog.normalized_record_id)
            .where(
                NormalizedRecord.dataset_id == ds.id,
                NormalizedRecord.is_current.is_(True),
                TransformationLog.target_field.in_(targets),
                (TransformationLog.mapping_id.in_(superseded_mapping_ids))
                | (TransformationLog.transformation_type == "no_mapping_null"),
            )
        ).all()
        ids |= set(stale)
        if ids:
            result[ds.id] = sorted(ids)
    return result


def run_recovery(db: Session, *, trigger: str, source_id: int | None, plan: dict[int, list[int] | None],
                 description: str, details: dict) -> RecoveryOperation:
    """plan: {dataset_id: record_ids or None for the full dataset}."""
    op = RecoveryOperation(source_id=source_id, trigger=trigger, description=description, details=details)
    db.add(op)
    db.flush()
    runs = []
    for dataset_id, record_ids in plan.items():
        if record_ids is not None and not record_ids:
            continue
        run = process_dataset(db, dataset_id, record_ids=record_ids, trigger=trigger, recovery_operation_id=op.id)
        runs.append(run)
        op.records_reprocessed += run.records_processed
        op.records_recovered += run.summary.get("resolved_from_quarantine", 0)
        op.records_still_failing += run.records_quarantined
    op.details = {**details, "processing_run_ids": [r.id for r in runs],
                  "datasets": sorted(plan), "records_valid": sum(r.records_valid for r in runs)}
    db.flush()
    return op


def correct_and_reprocess(db: Session, mapping_id: int, new_target: str | None, replacement_source_field: str | None,
                          reason: str | None, reprocess: bool = True) -> dict:
    wrong = mapping_service.get_active(db, mapping_id)
    old_target = wrong.target_field
    change = mapping_service.correct(db, mapping_id, new_target, replacement_source_field, reason=reason)
    targets = {t for t in (old_target, new_target) if t}
    plan = affected_records(db, wrong.source_id, change["fields"], targets, change["superseded_ids"])
    details = {
        "mapping_change": {
            "corrected_mapping_id": change["corrected"].id,
            "corrected_field": wrong.source_field,
            "old_target": old_target,
            "new_target": new_target,
            "replacement_mapping_id": change["replacement"].id if change["replacement"] else None,
            "replacement_source_field": replacement_source_field,
            "superseded_mapping_ids": change["superseded_ids"],
        },
        "affected_records": {str(k): len(v) for k, v in plan.items()},
    }
    op = None
    if reprocess:
        op = run_recovery(
            db, trigger="mapping_correction", source_id=wrong.source_id, plan=dict(plan),
            description=f"Corrected {wrong.source_field} -> {old_target}"
                        + (f"; {replacement_source_field} -> {old_target}" if replacement_source_field else ""),
            details=details,
        )
    return {"change": change, "affected": plan, "operation": op}


def reprocess(db: Session, source_id: int | None, dataset_id: int | None, mode: str) -> RecoveryOperation:
    if mode not in {"quarantined", "all"}:
        raise ProcessingError("mode must be 'quarantined' or 'all'.")
    if dataset_id is not None:
        datasets = [db.get(Dataset, dataset_id)]
        if not datasets[0]:
            raise ProcessingError(f"Dataset {dataset_id} not found.", status_code=404)
    elif source_id is not None:
        datasets = db.scalars(select(Dataset).where(Dataset.source_id == source_id).order_by(Dataset.id)).all()
    else:
        raise ProcessingError("Provide source_id or dataset_id.")
    plan: dict[int, list[int] | None] = {}
    for ds in datasets:
        plan[ds.id] = None if mode == "all" else sorted(_open_quarantine_ids(db, ds.id))
    resolved_source = source_id if source_id is not None else datasets[0].source_id
    return run_recovery(
        db, trigger="manual_reprocess", source_id=resolved_source, plan=plan,
        description=f"Reprocess {mode} records for " + (f"dataset {dataset_id}" if dataset_id else f"source {source_id}"),
        details={"mode": mode},
    )


def retry_quarantined(db: Session, quarantine_id: int) -> RecoveryOperation:
    q = db.get(QuarantinedRecord, quarantine_id)
    if not q:
        raise ProcessingError(f"Quarantined record {quarantine_id} not found.", status_code=404)
    if q.status != "open":
        raise ProcessingError(f"Quarantined record {quarantine_id} is already {q.status}.", status_code=409)
    ds = db.get(Dataset, q.dataset_id)
    return run_recovery(
        db, trigger="quarantine_retry", source_id=ds.source_id, plan={q.dataset_id: [q.source_record_id]},
        description=f"Retry quarantined record {quarantine_id}", details={"quarantine_id": quarantine_id},
    )
