"""Mapping engine: deterministic source-to-canonical field suggestions.

Confidence scoring (documented, no randomness):

  exact            1.00  source name equals the canonical name
  normalized_name  0.95  equal after case/separator normalisation (customerId -> customer_id)
  alias            0.90  matches a configured alias
  alias (ambiguous) 0.50 alias flagged ambiguous (e.g. 'id'); always needs review
  similarity       min(0.85, 0.60*name + 0.25*type + 0.15*value_pass_rate)
                   only considered when name similarity >= 0.5

The value pass rate is the share of the field's actual values that pass the
target's validation rules. Auto-approval requires an eligible method, confidence
at or above the policy threshold, no ambiguity, a compatible type, a value pass
rate at or above the policy threshold (exact name matches are exempt; their bad
values are quarantined per record), and no competing field for the same target. Everything
else stays 'suggested' until a person approves, rejects or corrects it.
"""
from typing import Any

from app.engines.validation import value_pass_rate
from app.utils.text import name_similarity, normalize_name

TYPE_FIT = {
    "identifier": {"string": 1.0, "integer": 1.0},
    "string": {"string": 1.0},
    "email": {"string": 1.0},
    "decimal": {"integer": 1.0, "float": 1.0},
    "phone": {"string": 1.0, "integer": 0.75},
}
SIMILARITY_MIN_NAME = 0.5
SIMILARITY_CAP = 0.85


def type_fit(target_type: str, inferred_type: str) -> float:
    if inferred_type in ("null", "mixed"):
        return 0.5
    return TYPE_FIT.get(target_type, {}).get(inferred_type, 0.0)


def resolve_by_name(source_field: str, canonical: dict) -> tuple[str, str, bool] | None:
    """Exact / normalized / alias resolution. Returns (target, method, ambiguous)."""
    norm = normalize_name(source_field)
    for f in canonical["fields"]:
        if source_field == f["name"]:
            return f["name"], "exact", False
    for f in canonical["fields"]:
        if norm == normalize_name(f["name"]):
            return f["name"], "normalized_name", False
    for f in canonical["fields"]:
        for alias in f.get("aliases", []):
            if norm == normalize_name(alias["name"]):
                return f["name"], "alias", bool(alias.get("ambiguous"))
    return None


def score_candidates(source_field: str, field_profile: dict, values: list[Any], canonical: dict) -> list[dict]:
    """All plausible canonical targets for one source field, best first."""
    out = []
    named = resolve_by_name(source_field, canonical)
    for f in canonical["fields"]:
        tfit = type_fit(f["type"], field_profile["inferred_type"])
        pass_rate = value_pass_rate(f, values)
        evidence = []
        ambiguous = False
        if named and named[0] == f["name"]:
            _, method, ambiguous = named
            confidence = {"exact": 1.0, "normalized_name": 0.95, "alias": 0.9}[method]
            if ambiguous:
                confidence = 0.5
                note = next(
                    (a.get("note") for a in f.get("aliases", []) if normalize_name(a["name"]) == normalize_name(source_field)),
                    None,
                )
                evidence.append(f"'{source_field}' is an ambiguous alias of {f['name']}" + (f": {note}" if note else "."))
            elif method == "exact":
                evidence.append("Field name matches the canonical name exactly.")
            elif method == "normalized_name":
                evidence.append(f"'{source_field}' normalises to '{f['name']}'.")
            else:
                evidence.append(f"'{source_field}' is a configured alias of {f['name']}.")
        else:
            names = [f["name"]] + [a["name"] for a in f.get("aliases", [])]
            best_name, nsim = max(((n, name_similarity(source_field, n)) for n in names), key=lambda x: x[1])
            if nsim < SIMILARITY_MIN_NAME:
                continue
            method = "similarity"
            confidence = min(
                SIMILARITY_CAP,
                round(0.60 * nsim + 0.25 * tfit + 0.15 * (pass_rate if pass_rate is not None else 0.0), 4),
            )
            evidence.append(f"Name similarity {nsim:.2f} with '{best_name}'.")
        evidence.append(f"Type fit {tfit:.2f} ({field_profile['inferred_type']} -> {f['type']}).")
        if pass_rate is None:
            evidence.append("No non-empty values to validate.")
        else:
            evidence.append(f"{pass_rate:.0%} of values pass {f['name']} validation.")
        out.append({
            "source_field": source_field,
            "target_field": f["name"],
            "method": method,
            "confidence": round(confidence, 4),
            "ambiguous": ambiguous,
            "type_fit": tfit,
            "value_pass_rate": pass_rate,
            "evidence": evidence,
        })
    out.sort(key=lambda c: (-c["confidence"], [f["name"] for f in canonical["fields"]].index(c["target_field"])))
    return out


def auto_approvable(candidate: dict, policies: dict) -> tuple[bool, str]:
    if candidate["method"] not in policies.get("auto_approve_methods", []):
        return False, f"Method '{candidate['method']}' always needs review."
    if candidate["ambiguous"]:
        return False, "Ambiguous alias; needs review."
    if candidate["confidence"] < policies.get("auto_approve_min_confidence", 1.0):
        return False, "Confidence below the auto-approval threshold."
    if candidate["type_fit"] == 0:
        return False, "Inferred type does not fit the target type."
    rate = candidate["value_pass_rate"]
    if candidate["method"] in policies.get("value_check_exempt_methods", []):
        return True, "Exact name match with compatible type; invalid values will be quarantined per record."
    if rate is None or rate < policies.get("auto_approve_min_value_pass_rate", 1.0):
        return False, "Too few values pass target validation to approve automatically."
    return True, "Meets the auto-approval policy."


def suggest_for_dataset(
    profile: dict,
    values_by_field: dict[str, list[Any]],
    canonical: dict,
    approved_targets_in_dataset: dict[str, str],
    skip_fields: set[str] | None = None,
) -> list[dict]:
    """Suggest a mapping for every source field that has no active mapping yet.

    approved_targets_in_dataset: {target: source_field} for already-approved
    mappings whose source field exists in this dataset (these block auto-approval
    of a competing field for the same target).
    skip_fields: fields that already have an active mapping (reused as history).
    """
    policies = canonical.get("policies", {})
    proposals = []
    for name in profile["field_order"]:
        if skip_fields and name in skip_fields:
            continue
        cands = score_candidates(name, profile["fields"][name], values_by_field.get(name, []), canonical)
        if not cands:
            continue
        best = dict(cands[0])
        best["alternatives"] = [
            {"target_field": c["target_field"], "confidence": c["confidence"], "method": c["method"]} for c in cands[1:4]
        ]
        ok, reason = auto_approvable(best, policies)
        best["auto_approve"] = ok
        best["policy_reason"] = reason
        proposals.append(best)

    by_target: dict[str, list[dict]] = {}
    for p in proposals:
        by_target.setdefault(p["target_field"], []).append(p)
    for target, group in by_target.items():
        existing = approved_targets_in_dataset.get(target)
        if existing:
            for p in group:
                if p["auto_approve"]:
                    p["auto_approve"] = False
                    p["policy_reason"] = f"'{existing}' is already approved for {target}; needs review."
        elif sum(p["auto_approve"] for p in group) > 1:
            others = ", ".join(p["source_field"] for p in group)
            for p in group:
                p["auto_approve"] = False
                p["policy_reason"] = f"Several fields ({others}) compete for {target}; needs review."
        if len(group) > 1:
            for p in group:
                p["competing_fields"] = [q["source_field"] for q in group if q is not p]
    return proposals
