"""Validation engine: deterministic per-field checks against the canonical schema.

Each checker returns the normalized value plus the list of safe transformations
applied, or errors. Invalid values are never replaced by defaults: an invalid
number stays invalid (it is not turned into 0) and the record is quarantined.
"""
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.utils.text import to_text

PLAIN_NUMBER_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")
GROUPED_NUMBER_RE = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9\s().\-]+$")
_WS_RE = re.compile(r"\s+")

MISSING = object()  # sentinel: key absent from the source record


@dataclass
class FieldResult:
    value: Any = None
    transformations: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _error(code: str, target: str, raw: Any, message: str, level: str = "record") -> dict:
    return {
        "code": code,
        "field": target,
        "value": None if raw is MISSING else to_text(raw, 120),
        "message": message,
        "level": level,
    }


def _is_blank(raw: Any) -> bool:
    return raw is MISSING or raw is None or (isinstance(raw, str) and raw.strip() == "")


def _clean_text(raw: str, result: FieldResult, collapse: bool = False) -> str:
    text = raw.strip()
    if collapse:
        text = _WS_RE.sub(" ", text)
    if text != raw:
        result.transformations.append("trim_whitespace")
    return text


def check_identifier(fdef: dict, raw: Any) -> FieldResult:
    res = FieldResult()
    name = fdef["name"]
    if isinstance(raw, bool) or isinstance(raw, (float, dict, list)):
        res.errors.append(_error("INVALID_TYPE", name, raw, f"{name} must be text or an integer, got {type(raw).__name__}."))
        return res
    if isinstance(raw, int):
        text = str(raw)
        res.transformations.append("number_to_string")
    else:
        text = _clean_text(str(raw), res)
    pattern = fdef.get("pattern")
    if pattern and not re.match(pattern, text):
        res.errors.append(_error("INVALID_FORMAT", name, raw, f"'{text}' is not a valid {name}."))
        return res
    res.value = text
    return res


def check_string(fdef: dict, raw: Any) -> FieldResult:
    res = FieldResult()
    name = fdef["name"]
    if isinstance(raw, (bool, dict, list)):
        res.errors.append(_error("INVALID_TYPE", name, raw, f"{name} must be text."))
        return res
    if isinstance(raw, (int, float)):
        res.value = str(raw)
        res.transformations.append("number_to_string")
        return res
    res.value = _clean_text(str(raw), res, collapse=True)
    return res


def check_email(fdef: dict, raw: Any) -> FieldResult:
    res = FieldResult()
    name = fdef["name"]
    if not isinstance(raw, str):
        res.errors.append(_error("INVALID_TYPE", name, raw, f"{name} must be text."))
        return res
    text = _clean_text(raw, res)
    if not EMAIL_RE.match(text):
        res.errors.append(_error("INVALID_FORMAT", name, raw, f"'{text}' is not a valid email address."))
        return res
    lowered = text.lower()
    if lowered != text:
        res.transformations.append("lowercase_email")
    res.value = lowered
    return res


def check_decimal(fdef: dict, raw: Any) -> FieldResult:
    res = FieldResult()
    name = fdef["name"]
    if isinstance(raw, bool) or isinstance(raw, (dict, list)):
        res.errors.append(_error("INVALID_TYPE", name, raw, f"{name} must be numeric."))
        return res
    if isinstance(raw, (int, float)):
        if isinstance(raw, float) and (math.isnan(raw) or math.isinf(raw)):
            res.errors.append(_error("INVALID_NUMBER", name, raw, f"{name} must be a finite number."))
            return res
        number = Decimal(str(raw))
    else:
        text = _clean_text(str(raw), res)
        if GROUPED_NUMBER_RE.match(text):
            text = text.replace(",", "")
            res.transformations.append("remove_thousands_separator")
        if not PLAIN_NUMBER_RE.match(text):
            res.errors.append(
                _error(
                    "INVALID_NUMBER",
                    name,
                    raw,
                    f"'{raw}' is not a number. The value was kept as-is and not converted to 0.",
                )
            )
            return res
        try:
            number = Decimal(text)
        except InvalidOperation:  # pragma: no cover - guarded by regex
            res.errors.append(_error("INVALID_NUMBER", name, raw, f"'{raw}' is not a number."))
            return res
        res.transformations.append("string_to_number")
    minimum = fdef.get("minimum")
    if minimum is not None and number < Decimal(str(minimum)):
        res.errors.append(_error("OUT_OF_RANGE", name, raw, f"{name} must be at least {minimum}."))
        return res
    res.value = int(number) if number == number.to_integral_value() else float(number)
    return res


def check_phone(fdef: dict, raw: Any) -> FieldResult:
    res = FieldResult()
    name = fdef["name"]
    if isinstance(raw, bool) or isinstance(raw, (float, dict, list)):
        res.errors.append(_error("INVALID_TYPE", name, raw, f"{name} must be text."))
        return res
    if isinstance(raw, int):
        text = str(raw)
        res.transformations.append("number_to_string")
    else:
        text = _clean_text(str(raw), res, collapse=True)
    digits = sum(ch.isdigit() for ch in text)
    if not PHONE_RE.match(text) or not 7 <= digits <= 15:
        res.errors.append(_error("INVALID_FORMAT", name, raw, f"'{text}' is not a valid phone number."))
        return res
    res.value = text
    return res


CHECKERS = {
    "identifier": check_identifier,
    "string": check_string,
    "email": check_email,
    "decimal": check_decimal,
    "phone": check_phone,
}


def check_field(fdef: dict, raw: Any) -> FieldResult:
    """Validate and normalize one canonical field value, applying the
    required/optional policy for missing values."""
    name = fdef["name"]
    if _is_blank(raw):
        if fdef.get("required"):
            return FieldResult(
                errors=[_error("MISSING_REQUIRED_FIELD", name, raw, f"Required field {name} is missing or empty.")]
            )
        res = FieldResult(value=None)
        res.transformations.append("missing_to_null" if raw is MISSING else "empty_to_null")
        return res
    checker = CHECKERS.get(fdef["type"])
    if checker is None:  # pragma: no cover - configuration error
        raise ValueError(f"Unknown canonical field type: {fdef['type']}")
    return checker(fdef, raw)


def value_pass_rate(fdef: dict, values: list[Any]) -> float | None:
    """Share of non-blank values that pass the target field's checks.
    None when there are no non-blank values to judge."""
    candidates = [v for v in values if not _is_blank(v)]
    if not candidates:
        return None
    passed = sum(1 for v in candidates if check_field({**fdef, "required": False}, v).ok)
    return round(passed / len(candidates), 4)


def duplicate_status(key: Any, payload: dict, seen: dict[Any, dict]) -> tuple[str | None, dict | None]:
    """Return ('DUPLICATE_RECORD' | 'CONFLICTING_RECORD' | None, first_seen_entry)."""
    if key is None or key not in seen:
        return None, None
    first = seen[key]
    if first["payload"] == payload:
        return "DUPLICATE_RECORD", first
    return "CONFLICTING_RECORD", first
