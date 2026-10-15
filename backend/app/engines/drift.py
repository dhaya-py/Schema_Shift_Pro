"""Drift detection: compares two schema versions of the same source.

Rename detection produces *candidates* only, scored by a documented weighted sum:

    score = 0.40 * name_similarity
          + 0.20 * type_compatibility      (1 same/widening, 0.75 mixed with compatible dominant
                                            type, 0.5 unknown/mixed, 0 incompatible)
          + 0.15 * value_kind_similarity   (overlap of email/phone/numeric/text value shares)
          + 0.05 * null_pattern_similarity (1 - |null rate difference|)
          + 0.20 * shared_canonical_target (both names resolve to the same canonical field
                                            through exact/alias rules or an approved mapping)

Pairs need score >= 0.55 and non-zero type compatibility. A candidate is never
applied by this engine; the mapping engine and its approval policy decide.
"""
from collections.abc import Callable

from app.utils.text import name_similarity

SEVERITY_ORDER = {"none": 0, "info": 1, "warning": 2, "breaking": 3}
RENAME_THRESHOLD = 0.55
LIKELY_RENAME = 0.75
VALUE_KINDS = ("numeric", "email", "phone", "boolean", "datetime", "text", "object")


def type_compatibility(old: str, new: str) -> tuple[float, str]:
    if old == new:
        return 1.0, "same"
    if "null" in (old, new):
        return 0.5, "unknown"
    if {old, new} == {"integer", "float"}:
        return 1.0, "widening" if (old, new) == ("integer", "float") else "narrowing"
    if "mixed" in (old, new):
        return 0.5, "mixed"
    return 0.0, "incompatible"


def rename_type_compatibility(old: dict, new: dict) -> tuple[float, str]:
    """Like type_compatibility, but a mixed field is judged by its dominant type
    (scored 0.75 at best, since some of its values do not fit)."""
    tcomp, label = type_compatibility(old["inferred_type"], new["inferred_type"])
    if label == "mixed":
        dcomp, _ = type_compatibility(old.get("dominant_type") or "null", new.get("dominant_type") or "null")
        if dcomp == 1.0:
            return 0.75, "mixed, dominant types compatible"
    return tcomp, label


def _kind_shares(field: dict) -> dict[str, float]:
    counts = {k: v for k, v in field.get("kind_counts", {}).items() if k != "null"}
    total = sum(counts.values())
    return {k: (counts.get(k, 0) / total if total else 0.0) for k in VALUE_KINDS}


def value_kind_similarity(a: dict, b: dict) -> float:
    sa, sb = _kind_shares(a), _kind_shares(b)
    if not any(sa.values()) or not any(sb.values()):
        return 0.5
    return round(1 - sum(abs(sa[k] - sb[k]) for k in VALUE_KINDS) / 2, 4)


def _null_rate(field: dict) -> float:
    total = field.get("null_count", 0) + field.get("non_null_count", 0)
    return field.get("null_count", 0) / total if total else 0.0


def _max_severity(items: list[str]) -> str:
    return max(items, key=lambda s: SEVERITY_ORDER[s], default="none")


def rename_candidates(
    removed: dict[str, dict],
    added: dict[str, dict],
    resolve_target: Callable[[str], str | None],
) -> list[dict]:
    candidates = []
    for old_name, old in removed.items():
        for new_name, new in added.items():
            tcomp, tlabel = rename_type_compatibility(old, new)
            if tcomp == 0:
                continue
            nsim = name_similarity(old_name, new_name)
            vsim = value_kind_similarity(old, new)
            nullsim = round(1 - abs(_null_rate(old) - _null_rate(new)), 4)
            t_old, t_new = resolve_target(old_name), resolve_target(new_name)
            shared = 1.0 if t_old and t_old == t_new else 0.0
            score = round(0.40 * nsim + 0.20 * tcomp + 0.15 * vsim + 0.05 * nullsim + 0.20 * shared, 4)
            if score < RENAME_THRESHOLD:
                continue
            evidence = [
                f"Name similarity {nsim:.2f}",
                f"Type {old['inferred_type']} -> {new['inferred_type']} ({tlabel})",
                f"Value-format similarity {vsim:.2f}",
                f"Null-rate similarity {nullsim:.2f}",
            ]
            if shared:
                evidence.append(f"Both names are known variants of canonical field '{t_old}'")
            candidates.append(
                {
                    "from_field": old_name,
                    "to_field": new_name,
                    "score": score,
                    "likelihood": "likely" if score >= LIKELY_RENAME else "possible",
                    "components": {
                        "name_similarity": nsim,
                        "type_compatibility": tcomp,
                        "value_kind_similarity": vsim,
                        "null_pattern_similarity": nullsim,
                        "shared_canonical_target": shared,
                    },
                    "evidence": evidence,
                    "status": "candidate",
                }
            )
    candidates.sort(key=lambda c: (-c["score"], c["from_field"], c["to_field"]))
    return candidates


def compare_schemas(
    previous: dict | None,
    current: dict,
    mapped_targets: dict[str, str],
    required_targets: set[str],
    resolve_target: Callable[[str], str | None],
) -> dict:
    """Build a drift report. `mapped_targets` maps source fields with an approved
    mapping to their canonical target; it drives severity."""
    cur_fields = current["fields"]
    if previous is None:
        mixed = [
            {"field": n, "type_counts": f["type_counts"], "severity": "warning",
             "recommended_action": f"Review mixed values in '{n}' before relying on its type."}
            for n, f in cur_fields.items() if f["inferred_type"] == "mixed"
        ]
        return {
            "is_initial": True,
            "added_fields": [],
            "removed_fields": [],
            "type_changes": [],
            "nullability_changes": [],
            "mixed_type_fields": mixed,
            "rename_candidates": [],
            "severity": _max_severity([m["severity"] for m in mixed]) if mixed else "none",
            "change_count": 0,
            "summary": f"Initial schema registered with {len(cur_fields)} field(s).",
        }

    prev_fields = previous["fields"]
    added_names = [n for n in current["field_order"] if n not in prev_fields]
    removed_names = [n for n in previous["field_order"] if n not in cur_fields]

    added = []
    for n in added_names:
        f = cur_fields[n]
        added.append({
            "field": n,
            "inferred_type": f["inferred_type"],
            "severity": "info",
            "evidence": f"Present in {f['present_count']} record(s); not in previous version.",
            "recommended_action": "Review the mapping suggestion for this new field.",
        })

    removed = []
    for n in removed_names:
        target = mapped_targets.get(n)
        breaking = target in required_targets if target else False
        removed.append({
            "field": n,
            "previous_type": prev_fields[n]["inferred_type"],
            "mapped_to": target,
            "severity": "breaking" if breaking else ("warning" if target else "info"),
            "evidence": "Field is not present in any record of the new dataset.",
            "recommended_action": (
                f"Required canonical field '{target}' loses its source. Check rename candidates or map another field."
                if breaking else
                (f"Canonical field '{target}' will be null unless a replacement is mapped." if target
                 else "No action needed unless downstream consumers use this field.")
            ),
        })

    type_changes, nullability_changes, mixed = [], [], []
    for n in current["field_order"]:
        if n not in prev_fields:
            continue
        old, new = prev_fields[n], cur_fields[n]
        tcomp, label = type_compatibility(old["inferred_type"], new["inferred_type"])
        # A field that becomes mixed is reported under mixed_type_fields only.
        if label not in ("same", "unknown") and new["inferred_type"] != "mixed":
            target = mapped_targets.get(n)
            if label in ("widening", "mixed"):  # widening, or mixed values became consistent
                sev = "info"
            else:
                sev = "breaking" if target else "warning"
            type_changes.append({
                "field": n,
                "previous_type": old["inferred_type"],
                "current_type": new["inferred_type"],
                "compatibility": label,
                "mapped_to": target,
                "severity": sev,
                "evidence": f"Type counts now {new['type_counts']}",
                "recommended_action": (
                    "Safe widening; numeric values still validate." if label == "widening"
                    else "Values are now consistent with one type." if label == "mixed"
                    else "Values may fail validation; records that fail will be quarantined, not coerced."
                ),
            })
        if not old["nullable"] and new["nullable"]:
            target = mapped_targets.get(n)
            sev = "warning" if target in required_targets else "info"
            nullability_changes.append({
                "field": n,
                "change": "became_nullable",
                "null_count": new["null_count"],
                "mapped_to": target,
                "severity": sev,
                "evidence": f"{new['null_count']} null/missing value(s); previous version had none.",
                "recommended_action": (
                    f"Records missing required '{target}' will be quarantined." if sev == "warning"
                    else "Missing values become null in the canonical output."
                ),
            })
        if new["inferred_type"] == "mixed" and old["inferred_type"] != "mixed":
            mixed.append({
                "field": n,
                "type_counts": new["type_counts"],
                "severity": "warning",
                "recommended_action": f"Inspect values of '{n}'; invalid ones will be quarantined.",
            })

    renames = rename_candidates(
        {n: prev_fields[n] for n in removed_names},
        {n: cur_fields[n] for n in added_names},
        resolve_target,
    )
    for item in removed:
        cands = [c for c in renames if c["from_field"] == item["field"]]
        if cands:
            item["recommended_action"] += f" Possible rename to '{cands[0]['to_field']}' (score {cands[0]['score']:.2f})."

    severities = [i["severity"] for i in added + removed + type_changes + nullability_changes + mixed]
    change_count = len(added) + len(removed) + len(type_changes) + len(nullability_changes) + len(mixed)
    parts = []
    if added:
        parts.append(f"{len(added)} added")
    if removed:
        parts.append(f"{len(removed)} removed")
    if type_changes:
        parts.append(f"{len(type_changes)} type change(s)")
    if nullability_changes:
        parts.append(f"{len(nullability_changes)} nullability change(s)")
    if mixed:
        parts.append(f"{len(mixed)} mixed-type field(s)")
    if renames:
        parts.append(f"{len(renames)} rename candidate(s)")
    return {
        "is_initial": False,
        "added_fields": added,
        "removed_fields": removed,
        "type_changes": type_changes,
        "nullability_changes": nullability_changes,
        "mixed_type_fields": mixed,
        "rename_candidates": renames,
        "severity": _max_severity(severities) if severities else "none",
        "change_count": change_count,
        "summary": ", ".join(parts) if parts else "No structural changes.",
    }
