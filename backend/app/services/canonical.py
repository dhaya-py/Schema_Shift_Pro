"""Demonstration canonical schema for customer data.

This is one example target model, not a universal schema. Field types, rules,
aliases and policies live in the definition (stored in the database), so the
engines read them instead of hardcoding customer-specific logic.
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CanonicalSchema

CUSTOMER_SCHEMA_NAME = "customer"

CUSTOMER_SCHEMA_V1: dict = {
    "name": CUSTOMER_SCHEMA_NAME,
    "description": "Demonstration customer model used by the Schema Shift prototype.",
    "fields": [
        {
            "name": "customer_id",
            "type": "identifier",
            "required": True,
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$",
            "description": "Stable customer identifier. Kept as text so leading zeros survive.",
            "aliases": [
                {"name": "customerId"},
                {"name": "cust_id"},
                {
                    "name": "id",
                    "ambiguous": True,
                    "note": "'id' can mean a customer, transaction or row id; needs review.",
                },
            ],
        },
        {
            "name": "customer_name",
            "type": "string",
            "required": True,
            "aliases": [{"name": "customerName"}, {"name": "full_name"}, {"name": "name"}],
        },
        {
            "name": "email",
            "type": "email",
            "required": False,
            "aliases": [{"name": "email_address"}],
        },
        {
            "name": "total_spent",
            "type": "decimal",
            "required": False,
            "minimum": 0,
            "aliases": [{"name": "lifetime_value"}, {"name": "amount_spent"}],
        },
        {
            "name": "phone",
            "type": "phone",
            "required": False,
            "aliases": [{"name": "phone_number"}, {"name": "mobile"}],
        },
    ],
    "policies": {
        # Records sharing this key within one dataset are duplicates/conflicts.
        "duplicate_key": "customer_id",
        # Auto-approval requires all of: method in this list, confidence at or
        # above the threshold, no ambiguity, and enough values passing validation.
        "auto_approve_methods": ["exact", "normalized_name", "alias"],
        "auto_approve_min_confidence": 0.9,
        "auto_approve_min_value_pass_rate": 0.8,
        # An exact name match is approved even if some values are bad: those
        # values are a record-level problem and get quarantined per record.
        "value_check_exempt_methods": ["exact"],
        "quarantine_invalid_records": True,
    },
}


def ensure_canonical_schema(db: Session) -> CanonicalSchema:
    existing = db.scalar(
        select(CanonicalSchema)
        .where(CanonicalSchema.name == CUSTOMER_SCHEMA_NAME)
        .order_by(CanonicalSchema.version_number.desc())
    )
    if existing and existing.schema_definition == CUSTOMER_SCHEMA_V1:
        return existing
    # A changed definition becomes a new version; earlier versions are kept.
    schema = CanonicalSchema(
        name=CUSTOMER_SCHEMA_NAME,
        schema_definition=CUSTOMER_SCHEMA_V1,
        version_number=(existing.version_number + 1) if existing else 1,
    )
    db.add(schema)
    db.flush()
    return schema


def get_canonical(db: Session) -> CanonicalSchema:
    schema = db.scalar(
        select(CanonicalSchema)
        .where(CanonicalSchema.name == CUSTOMER_SCHEMA_NAME)
        .order_by(CanonicalSchema.version_number.desc())
    )
    return schema or ensure_canonical_schema(db)


def field_defs(definition: dict) -> list[dict]:
    return definition["fields"]


def required_targets(definition: dict) -> set[str]:
    return {f["name"] for f in definition["fields"] if f.get("required")}
