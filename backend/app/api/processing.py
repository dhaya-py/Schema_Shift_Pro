from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import paginate, queue_events
from app.core.database import get_db
from app.models import (
    DataSource,
    Dataset,
    FieldMapping,
    NormalizedRecord,
    ProcessingRun,
    QuarantinedRecord,
    RecoveryOperation,
    SourceRecord,
    TransformationLog,
)
from app.schemas import (
    DatasetOut,
    Lineage,
    LineageStep,
    NormalizedOut,
    Page,
    QuarantineDetail,
    QuarantineOut,
    RecoveryOut,
    ReprocessRequest,
    RunOut,
    SourceOut,
    SourceRecordOut,
)
from app.services import recovery

router = APIRouter(tags=["processing"])


# ---------- Processing runs ----------
@router.get("/processing-runs", response_model=list[RunOut])
def list_runs(dataset_id: int | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(ProcessingRun).order_by(ProcessingRun.id.desc()).limit(paginate(limit, 0)[0])
    if dataset_id is not None:
        q = q.where(ProcessingRun.dataset_id == dataset_id)
    return db.scalars(q).all()


@router.get("/processing-runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(ProcessingRun, run_id)
    if not run:
        raise HTTPException(404, f"Processing run {run_id} not found.")
    return run


@router.get("/processing-runs/{run_id}/summary", summary="Run summary with validation errors (for n8n or the UI)")
def run_summary(run_id: int, db: Session = Depends(get_db)):
    run = get_run(run_id, db)
    ds = db.get(Dataset, run.dataset_id)
    quarantined = db.scalars(select(QuarantinedRecord).where(QuarantinedRecord.processing_run_id == run.id)
                             .order_by(QuarantinedRecord.id)).all()
    return {
        "run": RunOut.model_validate(run),
        "dataset": {"id": ds.id, "filename": ds.filename, "source_id": ds.source_id},
        "needs_attention": run.records_quarantined > 0 or bool(run.summary.get("schema_issues")),
        "quarantined": [
            {"id": q.id, "source_record_id": q.source_record_id, "status": q.status,
             "codes": sorted({e.get("code") for e in q.errors}), "failure_reason": q.failure_reason}
            for q in quarantined
        ],
    }


# ---------- Normalized records + lineage ----------
@router.get("/normalized-records", response_model=Page)
def list_normalized(dataset_id: int | None = None, include_superseded: bool = False, limit: int = 100,
                    offset: int = 0, db: Session = Depends(get_db)):
    limit, offset = paginate(limit, offset)
    cond = []
    if dataset_id is not None:
        cond.append(NormalizedRecord.dataset_id == dataset_id)
    if not include_superseded:
        cond.append(NormalizedRecord.is_current.is_(True))
    total = db.scalar(select(func.count(NormalizedRecord.id)).where(*cond)) or 0
    rows = db.scalars(select(NormalizedRecord).where(*cond).order_by(NormalizedRecord.id.desc())
                      .limit(limit).offset(offset)).all()
    return Page(total=total, items=[NormalizedOut.model_validate(r) for r in rows])


@router.get("/normalized-records/{record_id}", response_model=NormalizedOut)
def get_normalized(record_id: int, db: Session = Depends(get_db)):
    nr = db.get(NormalizedRecord, record_id)
    if not nr:
        raise HTTPException(404, f"Normalized record {record_id} not found.")
    return nr


@router.get("/normalized-records/{record_id}/lineage", response_model=Lineage,
            summary="Where each value came from, which mapping version produced it, and how")
def lineage(record_id: int, db: Session = Depends(get_db)):
    nr = get_normalized(record_id, db)
    src = db.get(SourceRecord, nr.source_record_id)
    ds = db.get(Dataset, nr.dataset_id)
    logs = db.scalars(select(TransformationLog).where(TransformationLog.normalized_record_id == nr.id)
                      .order_by(TransformationLog.id)).all()
    mapping_ids = {log.mapping_id for log in logs if log.mapping_id}
    maps = {m.id: m for m in db.scalars(select(FieldMapping).where(FieldMapping.id.in_(mapping_ids))).all()}
    steps = []
    for log in logs:
        step = LineageStep.model_validate(log)
        m = maps.get(log.mapping_id)
        if m:
            step.mapping_method, step.mapping_decided_by = m.mapping_method, m.decided_by
        steps.append(step)
    history = db.scalars(select(NormalizedRecord).where(NormalizedRecord.source_record_id == nr.source_record_id)
                         .order_by(NormalizedRecord.id)).all()
    return Lineage(
        normalized_record=NormalizedOut.model_validate(nr),
        source_record=SourceRecordOut.model_validate(src),
        dataset=DatasetOut.model_validate(ds),
        source=SourceOut.model_validate(db.get(DataSource, ds.source_id)),
        processing_run=RunOut.model_validate(db.get(ProcessingRun, nr.processing_run_id)),
        steps=steps,
        history=[NormalizedOut.model_validate(h) for h in history],
    )


# ---------- Quarantine ----------
@router.get("/quarantine", response_model=Page)
def list_quarantine(status: str | None = "open", dataset_id: int | None = None, source_id: int | None = None,
                    limit: int = 100, offset: int = 0, db: Session = Depends(get_db)):
    limit, offset = paginate(limit, offset)
    cond = []
    if status and status != "all":
        cond.append(QuarantinedRecord.status == status)
    if dataset_id is not None:
        cond.append(QuarantinedRecord.dataset_id == dataset_id)
    if source_id is not None:
        cond.append(QuarantinedRecord.dataset_id.in_(select(Dataset.id).where(Dataset.source_id == source_id)))
    total = db.scalar(select(func.count(QuarantinedRecord.id)).where(*cond)) or 0
    rows = db.scalars(select(QuarantinedRecord).where(*cond).order_by(QuarantinedRecord.id.desc())
                      .limit(limit).offset(offset)).all()
    return Page(total=total, items=[QuarantineOut.model_validate(r) for r in rows])


@router.get("/quarantine/{quarantine_id}", response_model=QuarantineDetail)
def get_quarantine(quarantine_id: int, db: Session = Depends(get_db)):
    q = db.get(QuarantinedRecord, quarantine_id)
    if not q:
        raise HTTPException(404, f"Quarantined record {quarantine_id} not found.")
    src = db.get(SourceRecord, q.source_record_id)
    ds = db.get(Dataset, q.dataset_id)
    history = db.scalars(select(QuarantinedRecord).where(QuarantinedRecord.source_record_id == q.source_record_id)
                         .order_by(QuarantinedRecord.id)).all()
    return QuarantineDetail(
        **QuarantineOut.model_validate(q).model_dump(), record_index=src.record_index,
        dataset_filename=ds.filename, source_id=ds.source_id,
        history=[QuarantineOut.model_validate(h) for h in history],
    )


def _recovery_events(op: RecoveryOperation) -> list[tuple[str, dict]]:
    return [("reprocessing.completed", {
        "recovery_operation_id": op.id, "trigger": op.trigger, "records_reprocessed": op.records_reprocessed,
        "records_recovered": op.records_recovered, "records_still_failing": op.records_still_failing,
    })]


@router.post("/quarantine/{quarantine_id}/retry", response_model=RecoveryOut,
             summary="Reprocess one quarantined record with the current mappings")
def retry_quarantine(quarantine_id: int, background: BackgroundTasks, db: Session = Depends(get_db)):
    op = recovery.retry_quarantined(db, quarantine_id)
    db.commit()
    queue_events(background, _recovery_events(op))
    return op


# ---------- Recovery ----------
@router.post("/reprocessing", response_model=RecoveryOut,
             summary="Reprocess quarantined (or all) records of a source or dataset")
def reprocess(body: ReprocessRequest, background: BackgroundTasks, db: Session = Depends(get_db)):
    op = recovery.reprocess(db, body.source_id, body.dataset_id, body.mode)
    db.commit()
    queue_events(background, _recovery_events(op))
    return op


@router.get("/recovery-operations", response_model=list[RecoveryOut])
def list_recovery(source_id: int | None = None, db: Session = Depends(get_db)):
    q = select(RecoveryOperation).order_by(RecoveryOperation.id.desc())
    if source_id is not None:
        q = q.where(RecoveryOperation.source_id == source_id)
    return db.scalars(q).all()
