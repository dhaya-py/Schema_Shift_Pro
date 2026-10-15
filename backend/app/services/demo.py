"""Deterministic demo loader.

Only sources flagged is_demo=True are deleted on reload. The loader goes through
the same ingestion, mapping and processing services as real uploads; the one
scripted human action (Scenario J's wrong mapping) is labelled as simulated.
"""
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DataSource
from app.services import ingestion
from app.services import mappings as mapping_service
from app.services.processing import process_dataset

DEMO_STEPS = [
    {"source": "CRM Customers", "type": "database_export", "file": "crm_customers_v1.csv",
     "scenarios": ["A: stable initial schema", "E: numeric strings normalized safely"]},
    {"source": "Shop API", "type": "api", "file": "shop_api_customers.json",
     "scenarios": ["B: second source with different field names"]},
    {"source": "CRM Customers", "type": "database_export", "file": "crm_customers_v2.csv",
     "scenarios": ["C: additive phone field", "F: 'unknown' total_spent", "G: missing customer_id",
                   "H: duplicate record"]},
    {"source": "CRM Customers", "type": "database_export", "file": "crm_customers_v3.csv",
     "scenarios": ["D: total_spent renamed to lifetime_value"]},
    {"source": "Legacy POS", "type": "file", "file": "legacy_pos_export.csv",
     "scenarios": ["I: ambiguous 'id' field needs review"]},
    {"source": "Web Signups", "type": "api", "file": "web_signups.json",
     "scenarios": ["J: incorrect mapping, to be corrected and reprocessed"],
     "simulated_wrong_mapping": ("spend_band", "total_spent")},
]
DEMO_SOURCES = sorted({s["source"] for s in DEMO_STEPS})


def reset_demo(db: Session) -> int:
    clash = db.scalars(select(DataSource.name).where(DataSource.name.in_(DEMO_SOURCES),
                                                     DataSource.is_demo.is_(False))).all()
    if clash:
        raise ingestion.IngestionError(
            f"Non-demo source(s) {', '.join(clash)} use demo names; rename them before loading the demo.", 409
        )
    # Database-level ON DELETE CASCADE removes the demo sources' datasets, records,
    # runs, outputs, quarantine entries, logs, mappings and recovery operations.
    return db.execute(delete(DataSource).where(DataSource.is_demo.is_(True))).rowcount


def load_demo(db: Session, samples_dir: str | None = None) -> dict:
    folder = Path(samples_dir or get_settings().samples_dir)
    removed = reset_demo(db)
    db.flush()
    steps = []
    for step in DEMO_STEPS:
        content = (folder / step["file"]).read_bytes()
        parsed = ingestion.parse_upload(step["file"], content)
        source = ingestion.get_or_create_source(
            db, None, step["source"], source_type=step["type"],
            description="Demo source (reloaded by the demo loader).", is_demo=True,
        )
        dataset, drift = ingestion.ingest_dataset(db, source, step["file"], parsed)
        note = None
        if step.get("simulated_wrong_mapping"):
            field, target = step["simulated_wrong_mapping"]
            mapping_service.create_manual(
                db, source.id, field, target, decided_by="demo-analyst (simulated)",
                note="Demo: simulated incorrect analyst mapping, to be corrected live.",
            )
            note = f"Simulated analyst mapped {field} -> {target} (incorrect)."
        run = process_dataset(db, dataset.id, trigger="demo_load")
        steps.append({
            "file": step["file"], "source": source.name, "source_id": source.id, "dataset_id": dataset.id,
            "scenarios": step["scenarios"], "drift_severity": drift.severity,
            "drift_summary": drift.report.get("summary"), "run_id": run.id,
            "records": run.records_processed, "valid": run.records_valid,
            "quarantined": run.records_quarantined, "error_counts": run.summary.get("error_counts", {}),
            "note": note,
        })
    db.commit()
    return {"removed_demo_sources": removed, "steps": steps}
