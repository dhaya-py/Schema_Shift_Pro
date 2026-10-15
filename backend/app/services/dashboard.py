"""Dashboard metrics. Every number is computed from database rows at request time."""
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    DataSource,
    Dataset,
    DriftReport,
    FieldMapping,
    NormalizedRecord,
    ProcessingRun,
    QuarantinedRecord,
    RecoveryOperation,
    SchemaVersion,
    SourceRecord,
)


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def summary(db: Session) -> dict:
    recent = db.scalars(select(ProcessingRun).order_by(ProcessingRun.id.desc()).limit(8)).all()
    names = dict(db.execute(select(Dataset.id, Dataset.filename)).all())
    return {
        "sources": _count(db, select(func.count(DataSource.id))),
        "datasets": _count(db, select(func.count(Dataset.id))),
        "source_records": _count(db, select(func.count(SourceRecord.id))),
        "schema_versions": _count(db, select(func.count(SchemaVersion.id))),
        "processing_runs": _count(db, select(func.count(ProcessingRun.id))),
        "normalized_records": _count(
            db, select(func.count(NormalizedRecord.id)).where(NormalizedRecord.is_current.is_(True))
        ),
        "quarantined_open": _count(
            db, select(func.count(QuarantinedRecord.id)).where(QuarantinedRecord.status == "open")
        ),
        "quarantined_resolved": _count(
            db, select(func.count(QuarantinedRecord.id)).where(QuarantinedRecord.status == "resolved")
        ),
        "schema_changes_detected": _count(db, select(func.sum(DriftReport.change_count))),
        "breaking_drift_reports": _count(
            db, select(func.count(DriftReport.id)).where(DriftReport.severity == "breaking")
        ),
        "mappings_pending_review": _count(
            db, select(func.count(FieldMapping.id)).where(
                FieldMapping.is_active.is_(True), FieldMapping.mapping_status == "suggested"
            )
        ),
        "recovery_operations": _count(db, select(func.count(RecoveryOperation.id))),
        "recent_runs": [
            {
                "id": r.id, "dataset_id": r.dataset_id, "dataset": names.get(r.dataset_id), "status": r.status,
                "trigger": r.trigger, "records_processed": r.records_processed, "records_valid": r.records_valid,
                "records_quarantined": r.records_quarantined, "started_at": r.started_at,
            }
            for r in recent
        ],
    }


def data_quality(db: Session) -> dict:
    codes: Counter = Counter()
    for errors in db.scalars(select(QuarantinedRecord.errors).where(QuarantinedRecord.status == "open")).all():
        for code in {e.get("code") for e in errors or []}:
            codes[code] += 1
    per_dataset = []
    for ds in db.scalars(select(Dataset).order_by(Dataset.id)).all():
        valid = _count(db, select(func.count(NormalizedRecord.id)).where(
            NormalizedRecord.dataset_id == ds.id, NormalizedRecord.is_current.is_(True)))
        quarantined = _count(db, select(func.count(QuarantinedRecord.id)).where(
            QuarantinedRecord.dataset_id == ds.id, QuarantinedRecord.status == "open"))
        per_dataset.append({
            "dataset_id": ds.id, "filename": ds.filename, "source_id": ds.source_id,
            "record_count": ds.record_count, "valid": valid, "quarantined": quarantined,
            "unprocessed": max(ds.record_count - valid - quarantined, 0), "status": ds.processing_status,
        })
    return {
        "open_quarantine_by_reason": [{"code": c, "records": n} for c, n in codes.most_common()],
        "datasets": per_dataset,
    }


def schema_changes(db: Session) -> dict:
    sources = dict(db.execute(select(DataSource.id, DataSource.name)).all())
    files = dict(db.execute(select(Dataset.id, Dataset.filename)).all())
    reports = db.scalars(select(DriftReport).order_by(DriftReport.id.desc()).limit(50)).all()
    return {
        "reports": [
            {
                "id": r.id, "source_id": r.source_id, "source": sources.get(r.source_id),
                "dataset_id": r.dataset_id, "dataset": files.get(r.dataset_id), "severity": r.severity,
                "change_count": r.change_count, "summary": r.report.get("summary"),
                "added": len(r.report.get("added_fields", [])),
                "removed": len(r.report.get("removed_fields", [])),
                "type_changes": len(r.report.get("type_changes", [])),
                "rename_candidates": len(r.report.get("rename_candidates", [])),
                "created_at": r.created_at,
            }
            for r in reports
        ]
    }
