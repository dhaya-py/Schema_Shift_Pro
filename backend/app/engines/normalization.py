"""Normalization engine: builds a canonical record from an original record.

The original payload is read, never modified. Each canonical field produces a
lineage entry (source field, original value, transformed value, transformation
types, mapping id/version). A record with any error is not normalized; its
errors are returned so the caller can quarantine it.
"""
from dataclasses import dataclass, field
from typing import Any

from app.engines.validation import MISSING, check_field
from app.utils.text import to_text


@dataclass
class ResolvedMapping:
    mapping_id: int
    version: int
    source_field: str
    target_field: str
    method: str


@dataclass
class RecordOutcome:
    normalized: dict | None
    lineage: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def normalize_record(
    payload: dict,
    canonical: dict,
    mappings: dict[str, ResolvedMapping],
    schema_errors: dict[str, dict] | None = None,
    pending: dict[str, list[str]] | None = None,
) -> RecordOutcome:
    """mappings: {target_field: ResolvedMapping} usable for this dataset.
    schema_errors: {target_field: error} for targets blocked at schema level
    (unmapped required field, conflicting mappings).
    pending: {target_field: [source_field]} suggestions awaiting review for
    targets without an approved mapping. A record that has a value in such a
    field is quarantined instead of silently getting null."""
    schema_errors = schema_errors or {}
    pending = pending or {}
    normalized: dict[str, Any] = {}
    lineage: list[dict] = []
    errors: list[dict] = []

    for fdef in canonical["fields"]:
        target = fdef["name"]
        if target in schema_errors:
            if fdef.get("required") or schema_errors[target]["code"] == "CONFLICTING_MAPPING":
                errors.append(dict(schema_errors[target]))
                continue
        m = mappings.get(target)
        if m is None:
            waiting = [f for f in pending.get(target, []) if not _blank(payload.get(f))]
            if waiting and not fdef.get("required"):
                errors.append({
                    "code": "MAPPING_PENDING_REVIEW",
                    "field": target,
                    "source_field": waiting[0],
                    "value": to_text(payload.get(waiting[0]), 120),
                    "message": f"'{waiting[0]}' has a value but its mapping to {target} is awaiting review.",
                    "level": "schema",
                })
                continue
            if fdef.get("required"):
                errors.append({
                    "code": "UNMAPPED_REQUIRED_FIELD",
                    "field": target,
                    "value": None,
                    "message": f"No approved mapping provides required field {target}.",
                    "level": "schema",
                })
                continue
            normalized[target] = None
            lineage.append({
                "source_field": None, "target_field": target, "original_value": None,
                "transformed_value": None, "transformation_type": "no_mapping_null",
                "mapping_id": None, "mapping_version": None,
            })
            continue

        raw = payload.get(m.source_field, MISSING)
        result = check_field(fdef, raw)
        if not result.ok:
            for e in result.errors:
                e["source_field"] = m.source_field
                e["mapping_id"] = m.mapping_id
                e["mapping_version"] = m.version
            errors.extend(result.errors)
            continue
        steps = (["rename"] if m.source_field != target else []) + result.transformations
        normalized[target] = result.value
        lineage.append({
            "source_field": m.source_field,
            "target_field": target,
            "original_value": None if raw is MISSING else to_text(raw),
            "transformed_value": to_text(result.value),
            "transformation_type": "+".join(steps) if steps else "direct_copy",
            "mapping_id": m.mapping_id,
            "mapping_version": m.version,
        })

    if errors:
        return RecordOutcome(normalized=None, lineage=[], errors=errors)
    return RecordOutcome(normalized=normalized, lineage=lineage, errors=[])


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def summarize_errors(errors: list[dict]) -> str:
    return "; ".join(f"{e['code']}: {e['message']}" for e in errors)
