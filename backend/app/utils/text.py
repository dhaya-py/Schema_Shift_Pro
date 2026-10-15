"""Deterministic helpers for field-name comparison and hashing."""
import hashlib
import json
import re
from difflib import SequenceMatcher
from typing import Any

_CAMEL_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """customerId / Customer-ID / customer id -> customer_id."""
    s = _CAMEL_1.sub(r"\1_\2", name.strip())
    s = _CAMEL_2.sub(r"\1_\2", s).lower()
    return _NON_ALNUM.sub("_", s).strip("_")


def name_tokens(name: str) -> set[str]:
    return {t for t in normalize_name(name).split("_") if t}


def name_similarity(a: str, b: str) -> float:
    """Average of character-sequence similarity and token overlap (0..1).

    Tokens are compared with a 4-character stem so 'spend' and 'spent' or
    'customer' and 'customers' count as overlapping.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    seq = SequenceMatcher(None, na, nb).ratio()
    ta = {t[:4] for t in name_tokens(a)}
    tb = {t[:4] for t in name_tokens(b)}
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return round((seq + jaccard) / 2, 4)


def stable_hash(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def to_text(value: Any, limit: int = 500) -> str | None:
    """Render a raw value for logs/evidence without losing its type information."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"
