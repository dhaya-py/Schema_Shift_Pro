"""Schema profiler: infers field types and format observations from all values.

Type inference never relies on one example. Every non-null value is classified,
and a field gets a single type only when all non-null values agree (integer and
float merge to float). Otherwise the field is reported as 'mixed' together with
the dominant type and the per-type counts.
"""
import math
import re
from collections import Counter
from typing import Any

from app.utils.text import to_text

INT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")
GROUPED_NUMBER_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
BOOL_WORDS = {"true", "false"}
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_CHARS_RE = re.compile(r"^\+?[0-9\s().\-]+$")

MAX_EXAMPLES = 5
LOW_SAMPLE_SIZE = 3


def is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _phone_like(text: str) -> bool:
    digits = sum(ch.isdigit() for ch in text)
    has_separator = any(ch in "+-() ." for ch in text)
    return bool(PHONE_CHARS_RE.match(text)) and 7 <= digits <= 15 and has_separator


def classify_value(value: Any) -> tuple[str, str]:
    """Return (data_type, value_kind) for one value.

    data_type: null | boolean | integer | float | datetime | string | object
    value_kind (format observation): null | boolean | numeric | datetime | email | phone | text | object
    Strings are classified by content, so CSV text and JSON values are comparable.
    """
    if is_missing(value):
        return "null", "null"
    if isinstance(value, bool):
        return "boolean", "boolean"
    if isinstance(value, int):
        return "integer", "numeric"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "string", "text"
        return "float", "numeric"
    if isinstance(value, (dict, list)):
        return "object", "object"
    text = str(value).strip()
    if text.lower() in BOOL_WORDS:
        return "boolean", "boolean"
    if INT_RE.match(text):
        digits = text.lstrip("+-")
        if len(digits) > 1 and digits.startswith("0"):
            # Leading zeros signal an identifier/code; treat as text so they are kept.
            return "string", "numeric"
        return "integer", "numeric"
    if FLOAT_RE.match(text) or GROUPED_NUMBER_RE.match(text):
        return "float", "numeric"
    if DATETIME_RE.match(text):
        return "datetime", "datetime"
    if EMAIL_RE.match(text):
        return "string", "email"
    if _phone_like(text):
        return "string", "phone"
    return "string", "text"


def _resolve_type(type_counts: Counter) -> tuple[str, str | None]:
    """Return (inferred_type, dominant_type)."""
    non_null = {t: c for t, c in type_counts.items() if t != "null"}
    if not non_null:
        return "null", None
    kinds = set(non_null)
    if kinds == {"integer", "float"}:
        return "float", "float"
    dominant = max(sorted(non_null), key=lambda t: non_null[t])
    if len(kinds) == 1:
        return dominant, dominant
    return "mixed", dominant


def profile_field(name: str, values: list[Any], present_count: int, record_count: int) -> dict:
    type_counts: Counter = Counter()
    kind_counts: Counter = Counter()
    distinct: set[str] = set()
    examples: list[str] = []
    observations: list[str] = []
    padded = leading_zero = grouped_numbers = text_encoded_numbers = 0

    for value in values:
        dtype, kind = classify_value(value)
        type_counts[dtype] += 1
        kind_counts[kind] += 1
        if dtype == "null":
            continue
        rendered = to_text(value, 80) or ""
        distinct.add(rendered)
        if len(examples) < MAX_EXAMPLES and rendered not in examples:
            examples.append(rendered)
        if isinstance(value, str):
            if value != value.strip():
                padded += 1
            stripped = value.strip()
            if INT_RE.match(stripped) and len(stripped.lstrip("+-")) > 1 and stripped.lstrip("+-").startswith("0"):
                leading_zero += 1
            if GROUPED_NUMBER_RE.match(stripped):
                grouped_numbers += 1
            if kind == "numeric":
                text_encoded_numbers += 1

    missing_keys = record_count - present_count
    null_count = type_counts.get("null", 0) + missing_keys
    non_null = sum(c for t, c in type_counts.items() if t != "null")
    inferred, dominant = _resolve_type(type_counts)

    if inferred == "mixed":
        parts = ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items()) if t != "null")
        observations.append(f"Mixed value types ({parts}); dominant type is {dominant}.")
    if non_null and non_null < LOW_SAMPLE_SIZE:
        observations.append(f"Only {non_null} non-null value(s); type inference has little evidence.")
    if missing_keys:
        observations.append(f"Field absent from {missing_keys} record(s) (counted as null, not a schema change).")
    if padded:
        observations.append(f"{padded} value(s) have leading/trailing whitespace.")
    if leading_zero:
        observations.append(f"{leading_zero} numeric-looking value(s) have leading zeros; treated as text.")
    if grouped_numbers:
        observations.append(f"{grouped_numbers} value(s) use thousands separators (e.g. 1,250).")
    if text_encoded_numbers and inferred in {"integer", "float"} and any(isinstance(v, str) for v in values):
        observations.append("Numbers are encoded as text.")
    for kind, label in (("email", "email addresses"), ("phone", "phone numbers")):
        if kind_counts.get(kind) and non_null:
            share = kind_counts[kind] / non_null
            observations.append(f"{share:.0%} of values look like {label}.")

    return {
        "name": name,
        "inferred_type": inferred,
        "dominant_type": dominant,
        "type_counts": dict(sorted(type_counts.items())),
        "kind_counts": dict(sorted(kind_counts.items())),
        "nullable": null_count > 0,
        "null_count": null_count,
        "non_null_count": non_null,
        "present_count": present_count,
        "distinct_count": len(distinct),
        "examples": examples,
        "observations": observations,
    }


def profile_records(records: list[dict], field_order: list[str]) -> dict:
    """Profile all fields in the dataset. field_order preserves CSV header / first-seen JSON key order."""
    record_count = len(records)
    fields = {}
    for name in field_order:
        present = [r[name] for r in records if name in r]
        fields[name] = profile_field(name, present, len(present), record_count)
    return {"record_count": record_count, "field_order": list(field_order), "fields": fields}


def schema_signature(profile: dict) -> dict[str, str]:
    """Structural signature used to decide whether a new schema version is needed."""
    return {name: f["inferred_type"] for name, f in profile["fields"].items()}
