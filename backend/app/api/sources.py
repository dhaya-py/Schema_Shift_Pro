from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import DataSource, Dataset, DriftReport, SchemaVersion
from app.schemas import DriftReportOut, SchemaVersionOut, SourceCreate, SourceDetail, SourceOut

router = APIRouter(prefix="/sources", tags=["sources"])


def _get(db: Session, source_id: int) -> DataSource:
    source = db.get(DataSource, source_id)
    if not source:
        raise HTTPException(404, f"Source {source_id} not found.")
    return source


@router.get("", response_model=list[SourceDetail], summary="List sources with their schema versions")
def list_sources(db: Session = Depends(get_db)):
    counts = dict(db.execute(select(Dataset.source_id, func.count(Dataset.id)).group_by(Dataset.source_id)).all())
    out = []
    for s in db.scalars(select(DataSource).order_by(DataSource.id)).all():
        out.append(SourceDetail(
            **SourceOut.model_validate(s).model_dump(),
            schema_versions=[SchemaVersionOut.model_validate(v) for v in s.schema_versions],
            dataset_count=counts.get(s.id, 0),
            latest_version=s.schema_versions[-1].version_number if s.schema_versions else None,
        ))
    return out


@router.post("", response_model=SourceOut, status_code=201, summary="Register a data source")
def create_source(body: SourceCreate, db: Session = Depends(get_db)):
    name = body.name.strip()
    if db.scalar(select(DataSource).where(DataSource.name == name)):
        raise HTTPException(409, f"A source named '{name}' already exists.")
    s = DataSource(name=name, source_type=body.source_type, description=body.description)
    db.add(s)
    db.commit()
    return s


@router.get("/{source_id}", response_model=SourceDetail)
def get_source(source_id: int, db: Session = Depends(get_db)):
    s = _get(db, source_id)
    count = db.scalar(select(func.count(Dataset.id)).where(Dataset.source_id == s.id)) or 0
    return SourceDetail(
        **SourceOut.model_validate(s).model_dump(),
        schema_versions=[SchemaVersionOut.model_validate(v) for v in s.schema_versions],
        dataset_count=count,
        latest_version=s.schema_versions[-1].version_number if s.schema_versions else None,
    )


@router.get("/{source_id}/schemas", response_model=list[SchemaVersionOut], summary="Schema version history")
def source_schemas(source_id: int, db: Session = Depends(get_db)):
    _get(db, source_id)
    return db.scalars(
        select(SchemaVersion).where(SchemaVersion.source_id == source_id).order_by(SchemaVersion.version_number)
    ).all()


@router.get("/{source_id}/drift", response_model=list[DriftReportOut], summary="Drift reports, newest first")
def source_drift(source_id: int, db: Session = Depends(get_db)):
    _get(db, source_id)
    return db.scalars(
        select(DriftReport).where(DriftReport.source_id == source_id).order_by(DriftReport.id.desc())
    ).all()
