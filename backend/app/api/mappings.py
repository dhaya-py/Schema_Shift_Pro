from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import queue_events
from app.core.database import get_db
from app.models import FieldMapping
from app.schemas import CorrectionResult, MappingCorrection, MappingCreate, MappingOut, MappingUpdate, RecoveryOut
from app.services import mappings as mapping_service
from app.services.canonical import get_canonical
from app.services.recovery import correct_and_reprocess

router = APIRouter(tags=["mappings"])


@router.get("/canonical-schema", summary="Active canonical schema definition")
def canonical_schema(db: Session = Depends(get_db)):
    c = get_canonical(db)
    return {"id": c.id, "name": c.name, "version_number": c.version_number, "definition": c.schema_definition}


@router.get("/mappings", response_model=list[MappingOut], summary="List mappings (current versions by default)")
def list_mappings(source_id: int | None = None, status: str | None = None, include_history: bool = False,
                  db: Session = Depends(get_db)):
    q = select(FieldMapping).order_by(FieldMapping.source_id, FieldMapping.source_field, FieldMapping.version)
    if source_id is not None:
        q = q.where(FieldMapping.source_id == source_id)
    if status:
        q = q.where(FieldMapping.mapping_status == status)
    if not include_history:
        q = q.where(FieldMapping.is_active.is_(True))
    return db.scalars(q).all()


@router.get("/mappings/{mapping_id}/history", response_model=list[MappingOut], summary="All versions of a field mapping")
def mapping_history(mapping_id: int, db: Session = Depends(get_db)):
    m = db.get(FieldMapping, mapping_id)
    if not m:
        raise HTTPException(404, f"Mapping {mapping_id} not found.")
    return db.scalars(select(FieldMapping).where(
        FieldMapping.source_id == m.source_id, FieldMapping.source_field == m.source_field
    ).order_by(FieldMapping.version)).all()


@router.post("/mappings", response_model=MappingOut, status_code=201, summary="Create or replace a mapping manually")
def create_mapping(body: MappingCreate, db: Session = Depends(get_db)):
    m = mapping_service.create_manual(db, body.source_id, body.source_field, body.target_field, note=body.note)
    db.commit()
    return m


@router.patch("/mappings/{mapping_id}", response_model=MappingOut, summary="Change target or status (new version)")
def update_mapping(mapping_id: int, body: MappingUpdate, db: Session = Depends(get_db)):
    m = mapping_service.update(db, mapping_id, body.target_field, body.mapping_status, note=body.note)
    db.commit()
    return m


@router.post("/mappings/{mapping_id}/approve", response_model=MappingOut, summary="Approve a suggested mapping")
def approve_mapping(mapping_id: int, db: Session = Depends(get_db)):
    m = mapping_service.approve(db, mapping_id)
    db.commit()
    return m


@router.post("/mappings/{mapping_id}/reject", response_model=MappingOut, summary="Reject a mapping")
def reject_mapping(mapping_id: int, db: Session = Depends(get_db)):
    m = mapping_service.reject(db, mapping_id)
    db.commit()
    return m


@router.post("/mappings/{mapping_id}/correct", response_model=CorrectionResult,
             summary="Correct a wrong mapping and reprocess affected records")
def correct_mapping(mapping_id: int, body: MappingCorrection, background: BackgroundTasks,
                    db: Session = Depends(get_db)):
    """Saves new mapping versions (old versions are kept), finds the records the
    change affects (open quarantine, or output produced by the old mapping) and
    reprocesses them. Returns the recovery operation with real counts."""
    result = correct_and_reprocess(db, mapping_id, body.new_target_field, body.replacement_source_field,
                                   body.reason, reprocess=body.reprocess)
    db.commit()
    change, op = result["change"], result["operation"]
    events = [("mapping.corrected", {
        "mapping_id": change["corrected"].id, "source_id": change["corrected"].source_id,
        "source_field": change["corrected"].source_field, "new_target": change["corrected"].target_field,
        "replacement_mapping_id": change["replacement"].id if change["replacement"] else None,
    })]
    if op:
        events.append(("reprocessing.completed", {
            "recovery_operation_id": op.id, "records_reprocessed": op.records_reprocessed,
            "records_recovered": op.records_recovered, "records_still_failing": op.records_still_failing,
        }))
    queue_events(background, events)
    return CorrectionResult(
        corrected_mapping=MappingOut.model_validate(change["corrected"]),
        replacement_mapping=MappingOut.model_validate(change["replacement"]) if change["replacement"] else None,
        affected_records={str(k): len(v) for k, v in result["affected"].items()},
        recovery=RecoveryOut.model_validate(op) if op else None,
    )
