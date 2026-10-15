"""Mapping service: persistence and versioning of field mappings.

Every change (approve, reject, correct, manual create) writes a new version row
and deactivates the previous one, so history is never overwritten.
"""
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.mapping import suggest_for_dataset
from app.models import DataSource, Dataset, FieldMapping
from app.services.canonical import get_canonical


class MappingError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def active_mappings(db: Session, source_id: int) -> dict[str, FieldMapping]:
    rows = db.scalars(
        select(FieldMapping).where(FieldMapping.source_id == source_id, FieldMapping.is_active.is_(True))
    ).all()
    return {m.source_field: m for m in rows}


def dataset_fields(dataset: Dataset) -> list[str]:
    return list((dataset.profile or {}).get("field_order", []))


def source_field_sets(db: Session, source_id: int) -> list[set[str]]:
    datasets = db.scalars(select(Dataset).where(Dataset.source_id == source_id)).all()
    return [set(dataset_fields(d)) for d in datasets]


def create_suggestions(
    db: Session,
    source: DataSource,
    dataset: Dataset,
    profile: dict,
    values_by_field: dict[str, list[Any]],
    canonical: dict,
    drift_report: dict,
) -> list[FieldMapping]:
    active = active_mappings(db, source.id)
    fields = set(profile["field_order"])
    approved_in_dataset = {
        m.target_field: m.source_field
        for m in active.values()
        if m.mapping_status == "approved" and m.target_field and m.source_field in fields
    }
    proposals = suggest_for_dataset(
        profile, values_by_field, canonical, approved_in_dataset, skip_fields=set(active)
    )
    renames = {c["to_field"]: c for c in drift_report.get("rename_candidates", [])}
    created = []
    for p in proposals:
        evidence = {
            "reasons": p["evidence"],
            "type_fit": p["type_fit"],
            "value_pass_rate": p["value_pass_rate"],
            "ambiguous": p["ambiguous"],
            "alternatives": p["alternatives"],
            "competing_fields": p.get("competing_fields", []),
            "policy": p["policy_reason"],
            "first_seen_dataset_id": dataset.id,
        }
        rename = renames.get(p["source_field"])
        if rename:
            evidence["rename_candidate"] = {
                "from_field": rename["from_field"], "score": rename["score"], "likelihood": rename["likelihood"]
            }
            evidence["reasons"].append(
                f"Drift detection: possible rename of '{rename['from_field']}' (score {rename['score']:.2f})."
            )
        m = FieldMapping(
            source_id=source.id,
            source_field=p["source_field"],
            target_field=p["target_field"],
            mapping_status="approved" if p["auto_approve"] else "suggested",
            confidence=p["confidence"],
            mapping_method=p["method"],
            version=1,
            is_active=True,
            decided_by="auto-policy" if p["auto_approve"] else None,
            evidence=evidence,
        )
        db.add(m)
        created.append(m)
    db.flush()
    return created


def _new_version(db: Session, current: FieldMapping, *, target_field: str | None, status: str,
                 method: str, decided_by: str, note: str | None, confidence: float | None = None) -> FieldMapping:
    current.is_active = False
    evidence = dict(current.evidence or {})
    history = list(evidence.get("change_notes", []))
    if note:
        history.append({"version": current.version + 1, "note": note})
    evidence["change_notes"] = history
    new = FieldMapping(
        source_id=current.source_id,
        source_field=current.source_field,
        target_field=target_field,
        mapping_status=status,
        confidence=current.confidence if confidence is None else confidence,
        mapping_method=method,
        version=current.version + 1,
        is_active=True,
        decided_by=decided_by,
        evidence=evidence,
        previous_mapping_id=current.id,
    )
    db.add(new)
    db.flush()
    return new


def _validate_target(db: Session, target: str | None) -> None:
    if target is None:
        return
    names = {f["name"] for f in get_canonical(db).schema_definition["fields"]}
    if target not in names:
        raise MappingError(f"'{target}' is not a canonical field. Choose one of: {', '.join(sorted(names))}.")


def _check_conflict(db: Session, source_id: int, source_field: str, target: str) -> None:
    """An approved target may have several source fields only if they never
    appear together in one dataset (e.g. a renamed field across versions)."""
    field_sets = source_field_sets(db, source_id)
    for other in active_mappings(db, source_id).values():
        if other.source_field == source_field or other.mapping_status != "approved" or other.target_field != target:
            continue
        if any(source_field in s and other.source_field in s for s in field_sets):
            raise MappingError(
                f"'{other.source_field}' is already approved for {target} in the same dataset. "
                f"Correct or reject that mapping first.",
                status_code=409,
            )


def get_active(db: Session, mapping_id: int) -> FieldMapping:
    m = db.get(FieldMapping, mapping_id)
    if not m:
        raise MappingError(f"Mapping {mapping_id} not found.", status_code=404)
    if not m.is_active:
        raise MappingError(
            f"Mapping {mapping_id} is version {m.version}, which is no longer current. Use the latest version.",
            status_code=409,
        )
    return m


def approve(db: Session, mapping_id: int, decided_by: str = "user") -> FieldMapping:
    m = get_active(db, mapping_id)
    if not m.target_field:
        raise MappingError("This mapping has no target field. Correct it to choose a target.")
    if m.mapping_status == "approved":
        return m
    _check_conflict(db, m.source_id, m.source_field, m.target_field)
    return _new_version(db, m, target_field=m.target_field, status="approved", method=m.mapping_method,
                        decided_by=decided_by, note=f"Approved {m.source_field} -> {m.target_field}.")


def reject(db: Session, mapping_id: int, decided_by: str = "user", note: str | None = None) -> FieldMapping:
    m = get_active(db, mapping_id)
    if m.mapping_status == "rejected":
        return m
    return _new_version(db, m, target_field=m.target_field, status="rejected", method=m.mapping_method,
                        decided_by=decided_by, note=note or f"Rejected {m.source_field} -> {m.target_field}.")


def update(db: Session, mapping_id: int, target_field: str | None, status: str | None,
           decided_by: str = "user", note: str | None = None) -> FieldMapping:
    m = get_active(db, mapping_id)
    _validate_target(db, target_field)
    new_status = status or ("approved" if target_field else "rejected")
    if new_status not in {"approved", "rejected", "suggested"}:
        raise MappingError("Status must be approved, rejected or suggested.")
    if new_status == "approved":
        if not target_field:
            raise MappingError("An approved mapping needs a target field.")
        _check_conflict(db, m.source_id, m.source_field, target_field)
    method = "manual" if target_field != m.target_field else m.mapping_method
    return _new_version(db, m, target_field=target_field, status=new_status, method=method, decided_by=decided_by,
                        note=note or f"Changed to {m.source_field} -> {target_field or '(none)'} ({new_status}).",
                        confidence=None if method == "manual" else m.confidence)


def create_manual(db: Session, source_id: int, source_field: str, target_field: str,
                  decided_by: str = "user", note: str | None = None) -> FieldMapping:
    if not db.get(DataSource, source_id):
        raise MappingError(f"Source {source_id} not found.", status_code=404)
    _validate_target(db, target_field)
    if not any(source_field in s for s in source_field_sets(db, source_id)):
        raise MappingError(f"No dataset of source {source_id} has a field named '{source_field}'.")
    _check_conflict(db, source_id, source_field, target_field)
    current = active_mappings(db, source_id).get(source_field)
    text = note or f"Manual mapping {source_field} -> {target_field}."
    if current:
        return _new_version(db, current, target_field=target_field, status="approved", method="manual",
                            decided_by=decided_by, note=text, confidence=None)
    m = FieldMapping(
        source_id=source_id, source_field=source_field, target_field=target_field, mapping_status="approved",
        confidence=None, mapping_method="manual", version=1, is_active=True, decided_by=decided_by,
        evidence={"reasons": ["Created manually."], "change_notes": [{"version": 1, "note": text}]},
    )
    db.add(m)
    db.flush()
    return m


def correct(db: Session, mapping_id: int, new_target: str | None, replacement_source_field: str | None,
            decided_by: str = "user", reason: str | None = None) -> dict:
    """Correct a wrong mapping. The wrong mapping gets a new version (re-targeted
    or rejected); optionally another source field is approved for the old target.
    Returns the superseded and new mapping rows."""
    wrong = get_active(db, mapping_id)
    old_target = wrong.target_field
    _validate_target(db, new_target)
    if replacement_source_field and replacement_source_field == wrong.source_field:
        raise MappingError("The replacement field must differ from the field being corrected.")
    superseded = [wrong.id]
    note = reason or "Mapping corrected."
    fixed = _new_version(
        db, wrong, target_field=new_target, status="approved" if new_target else "rejected", method="manual",
        decided_by=decided_by, note=f"{note} {wrong.source_field}: {old_target} -> {new_target or '(not mapped)'}.",
        confidence=None,
    )
    if new_target:
        _check_conflict(db, wrong.source_id, wrong.source_field, new_target)
    replacement = None
    if replacement_source_field:
        if not old_target:
            raise MappingError("The corrected mapping had no target to hand over.")
        current = active_mappings(db, wrong.source_id).get(replacement_source_field)
        if current:
            superseded.append(current.id)
        replacement = create_manual(
            db, wrong.source_id, replacement_source_field, old_target, decided_by=decided_by,
            note=f"{note} {replacement_source_field} now provides {old_target}.",
        )
    return {"corrected": fixed, "replacement": replacement, "superseded_ids": superseded,
            "fields": [f for f in (wrong.source_field, replacement_source_field) if f]}
