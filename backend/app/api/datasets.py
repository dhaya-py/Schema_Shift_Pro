from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import paginate, queue_events
from app.core.config import get_settings
from app.core.database import get_db
from app.models import Dataset, DriftReport, FieldMapping, NormalizedRecord, QuarantinedRecord, SourceRecord
from app.schemas import (
    CanonicalFieldOut,
    DatasetDetail,
    DatasetOut,
    DriftReportOut,
    MappingOut,
    MappingSuggestions,
    MappingSuggestionTarget,
    Page,
    RunOut,
    SourceOut,
    SourceRecordOut,
    UploadResult,
)
from app.services import ingestion
from app.services.canonical import get_canonical
from app.services.mappings import dataset_fields
from app.services.processing import ProcessingError, process_dataset, record_failed_run

router = APIRouter(prefix="/datasets", tags=["datasets"])


def _get(db: Session, dataset_id: int) -> Dataset:
    ds = db.get(Dataset, dataset_id)
    if not ds:
        raise HTTPException(404, f"Dataset {dataset_id} not found.")
    return ds


def run_events(run, dataset: Dataset) -> list[tuple[str, dict]]:
    data = {"processing_run_id": run.id, "dataset_id": dataset.id, "source_id": dataset.source_id,
            "status": run.status, "trigger": run.trigger, "records_processed": run.records_processed,
            "records_valid": run.records_valid, "records_quarantined": run.records_quarantined}
    events = [("processing.completed", data)]
    if run.records_quarantined:
        events.append(("quarantine.created", {**data, "error_counts": run.summary.get("error_counts", {})}))
    return events


@router.post("/upload", response_model=UploadResult, status_code=201, summary="Upload a CSV or JSON dataset")
async def upload_dataset(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    source_id: int | None = Form(default=None),
    source_name: str | None = Form(default=None),
    description: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """Stores original records, profiles the schema, registers a schema version
    when the structure changed, records drift and creates mapping suggestions.
    Processing is a separate step (POST /datasets/{id}/process)."""
    settings = get_settings()
    filename = ingestion.sanitize_filename(file.filename)
    content = await file.read(settings.max_upload_bytes + 1)
    parsed = ingestion.parse_upload(filename, content)
    source = ingestion.get_or_create_source(db, source_id, source_name, description=description)
    dataset, drift = ingestion.ingest_dataset(db, source, filename, parsed)
    db.commit()
    mappings = db.scalars(select(FieldMapping).where(FieldMapping.source_id == source.id,
                                                     FieldMapping.is_active.is_(True))).all()
    created = [m for m in mappings if (m.evidence or {}).get("first_seen_dataset_id") == dataset.id]
    events = [("dataset.uploaded", {"dataset_id": dataset.id, "source_id": source.id, "filename": filename,
                                    "record_count": dataset.record_count, "drift_severity": drift.severity,
                                    "drift_summary": drift.report.get("summary")})]
    if drift.severity == "breaking":
        events.append(("schema.breaking_change", {"dataset_id": dataset.id, "source_id": source.id,
                                                  "drift_report_id": drift.id, "summary": drift.report.get("summary")}))
    queue_events(background, events)
    return UploadResult(
        dataset=DatasetOut.model_validate(dataset),
        source=SourceOut.model_validate(source),
        drift=DriftReportOut.model_validate(drift),
        mappings_created=len(created),
        mappings_pending_review=sum(1 for m in created if m.mapping_status == "suggested"),
    )


@router.get("", response_model=list[DatasetOut], summary="List datasets")
def list_datasets(source_id: int | None = None, db: Session = Depends(get_db)):
    q = select(Dataset).order_by(Dataset.id.desc())
    if source_id is not None:
        q = q.where(Dataset.source_id == source_id)
    return db.scalars(q).all()


@router.get("/{dataset_id}", response_model=DatasetDetail)
def get_dataset(dataset_id: int, db: Session = Depends(get_db)):
    ds = _get(db, dataset_id)
    drift = db.scalar(select(DriftReport).where(DriftReport.dataset_id == ds.id).order_by(DriftReport.id.desc()))
    return DatasetDetail(
        **DatasetOut.model_validate(ds).model_dump(),
        source_name=ds.source.name,
        schema_version_number=ds.schema_version.version_number if ds.schema_version else None,
        profile=ds.profile,
        drift=DriftReportOut.model_validate(drift) if drift else None,
        open_quarantine=db.scalar(select(func.count(QuarantinedRecord.id)).where(
            QuarantinedRecord.dataset_id == ds.id, QuarantinedRecord.status == "open")) or 0,
        current_normalized=db.scalar(select(func.count(NormalizedRecord.id)).where(
            NormalizedRecord.dataset_id == ds.id, NormalizedRecord.is_current.is_(True))) or 0,
    )


@router.get("/{dataset_id}/records", response_model=Page, summary="Original (unmodified) records")
def dataset_records(dataset_id: int, limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    _get(db, dataset_id)
    limit, offset = paginate(limit, offset)
    total = db.scalar(select(func.count(SourceRecord.id)).where(SourceRecord.dataset_id == dataset_id)) or 0
    rows = db.scalars(select(SourceRecord).where(SourceRecord.dataset_id == dataset_id)
                      .order_by(SourceRecord.record_index).limit(limit).offset(offset)).all()
    return Page(total=total, items=[SourceRecordOut.model_validate(r) for r in rows])


@router.get("/{dataset_id}/schema", summary="Profiled schema of this dataset")
def dataset_schema(dataset_id: int, db: Session = Depends(get_db)):
    ds = _get(db, dataset_id)
    return {"dataset_id": ds.id, "source_id": ds.source_id,
            "schema_version_id": ds.schema_version_id,
            "schema_version_number": ds.schema_version.version_number if ds.schema_version else None,
            **ds.profile}


@router.get("/{dataset_id}/drift", response_model=DriftReportOut | None)
def dataset_drift(dataset_id: int, db: Session = Depends(get_db)):
    _get(db, dataset_id)
    return db.scalar(select(DriftReport).where(DriftReport.dataset_id == dataset_id).order_by(DriftReport.id.desc()))


@router.get("/{dataset_id}/mapping-suggestions", response_model=MappingSuggestions,
            summary="Mapping state for each canonical field, for this dataset's fields")
def mapping_suggestions(dataset_id: int, db: Session = Depends(get_db)):
    ds = _get(db, dataset_id)
    canonical = get_canonical(db).schema_definition
    fields = dataset_fields(ds)
    active = db.scalars(select(FieldMapping).where(
        FieldMapping.source_id == ds.source_id, FieldMapping.is_active.is_(True),
        FieldMapping.source_field.in_(fields))).all()
    targets = []
    used = set()
    for fdef in canonical["fields"]:
        rows = sorted((m for m in active if m.target_field == fdef["name"]),
                      key=lambda m: (m.mapping_status != "approved", -(m.confidence or 0)))
        approved = [m for m in rows if m.mapping_status == "approved"]
        suggested = [m for m in rows if m.mapping_status == "suggested"]
        used.update(m.source_field for m in rows)
        if len(approved) > 1:
            state = "conflict"
        elif approved:
            state = "applied_ready"
        elif suggested:
            state = "needs_review"
        else:
            state = "unmapped"
        targets.append(MappingSuggestionTarget(
            target_field=fdef["name"], target_type=fdef["type"], required=bool(fdef.get("required")),
            active_mapping=MappingOut.model_validate(approved[0]) if len(approved) == 1 else None,
            candidates=[MappingOut.model_validate(m) for m in rows], state=state,
        ))
    unmapped = [f for f in fields if f not in used]
    return MappingSuggestions(
        dataset_id=ds.id, source_id=ds.source_id, targets=targets, unmapped_source_fields=unmapped,
        canonical_fields=[CanonicalFieldOut(name=f["name"], type=f["type"], required=bool(f.get("required")))
                          for f in canonical["fields"]],
    )


@router.post("/{dataset_id}/process", response_model=RunOut, summary="Validate and normalize all records")
def process(dataset_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    ds = _get(db, dataset_id)
    try:
        run = process_dataset(db, ds.id, trigger="manual")
        db.commit()
    except ProcessingError:
        raise
    except Exception as exc:  # record the failure instead of leaving a half-written run
        db.rollback()
        record_failed_run(db, ds.id, "manual", f"{type(exc).__name__}: {exc}")
        db.commit()
        raise HTTPException(500, f"Processing failed and was rolled back: {type(exc).__name__}.") from exc
    queue_events(background, run_events(run, ds))
    return run
