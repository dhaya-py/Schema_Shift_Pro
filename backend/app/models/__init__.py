"""SQLAlchemy entities. Original data (SourceRecord) is kept separate from
normalized output (NormalizedRecord); nothing in processing mutates SourceRecord."""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    source_type: Mapped[str] = mapped_column(String(40), default="file")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    schema_versions: Mapped[list["SchemaVersion"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", order_by="SchemaVersion.version_number"
    )
    datasets: Mapped[list["Dataset"]] = relationship(back_populates="source", cascade="all, delete-orphan")
    mappings: Mapped[list["FieldMapping"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class SchemaVersion(Base):
    __tablename__ = "schema_versions"
    __table_args__ = (UniqueConstraint("source_id", "version_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    schema_definition: Mapped[dict] = mapped_column(JSON)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    change_summary: Mapped[str] = mapped_column(Text, default="")

    source: Mapped[DataSource] = relationship(back_populates="schema_versions")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    schema_version_id: Mapped[int | None] = mapped_column(ForeignKey("schema_versions.id", ondelete="SET NULL"), nullable=True)
    filename: Mapped[str] = mapped_column(String(255))
    format: Mapped[str] = mapped_column(String(10))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    processing_status: Mapped[str] = mapped_column(String(30), default="uploaded")
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    ingestion_warnings: Mapped[list] = mapped_column(JSON, default=list)

    source: Mapped[DataSource] = relationship(back_populates="datasets")
    schema_version: Mapped[SchemaVersion | None] = relationship()
    records: Mapped[list["SourceRecord"]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan", order_by="SourceRecord.record_index"
    )


class SourceRecord(Base):
    """Immutable copy of an ingested record. Never updated after insert."""

    __tablename__ = "source_records"
    __table_args__ = (UniqueConstraint("dataset_id", "record_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    record_index: Mapped[int] = mapped_column(Integer)
    original_payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    dataset: Mapped[Dataset] = relationship(back_populates="records")


class DriftReport(Base):
    __tablename__ = "drift_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    previous_version_id: Mapped[int | None] = mapped_column(ForeignKey("schema_versions.id", ondelete="SET NULL"), nullable=True)
    current_version_id: Mapped[int | None] = mapped_column(ForeignKey("schema_versions.id", ondelete="SET NULL"), nullable=True)
    severity: Mapped[str] = mapped_column(String(20))
    change_count: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CanonicalSchema(Base):
    __tablename__ = "canonical_schemas"
    __table_args__ = (UniqueConstraint("name", "version_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    schema_definition: Mapped[dict] = mapped_column(JSON)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FieldMapping(Base):
    """One row per version of a (source, source_field) mapping. Only the row with
    is_active=True is current; older versions are kept for history and lineage."""

    __tablename__ = "field_mappings"
    __table_args__ = (
        UniqueConstraint("source_id", "source_field", "version"),
        Index("ix_mapping_active", "source_id", "source_field", "is_active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), index=True)
    source_field: Mapped[str] = mapped_column(String(255))
    target_field: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # suggested | approved | rejected
    mapping_status: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # exact | normalized_name | alias | similarity | historical | manual
    mapping_method: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    decided_by: Mapped[str | None] = mapped_column(String(60), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    previous_mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_mappings.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    source: Mapped[DataSource] = relationship(back_populates="mappings")


class ProcessingRun(Base):
    __tablename__ = "processing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    # processing | completed | completed_with_quarantine | failed
    status: Mapped[str] = mapped_column(String(40))
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    recovery_operation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recovery_operations.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    records_valid: Mapped[int] = mapped_column(Integer, default=0)
    records_quarantined: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)


class NormalizedRecord(Base):
    """Canonical output. Reprocessing supersedes (is_current=False) rather than
    deleting, and a partial unique index keeps one current row per source record."""

    __tablename__ = "normalized_records"
    __table_args__ = (
        Index(
            "uq_normalized_current",
            "source_record_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"), index=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    normalized_payload: Mapped[dict] = mapped_column(JSON)
    validation_status: Mapped[str] = mapped_column(String(20), default="valid")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuarantinedRecord(Base):
    __tablename__ = "quarantined_records"
    __table_args__ = (Index("ix_quarantine_record_status", "source_record_id", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"), index=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id", ondelete="CASCADE"), index=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    failure_reason: Mapped[str] = mapped_column(Text)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    original_payload: Mapped[dict] = mapped_column(JSON)
    # open | resolved | superseded
    status: Mapped[str] = mapped_column(String(20), default="open")
    resolution_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TransformationLog(Base):
    __tablename__ = "transformation_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    processing_run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"), index=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id", ondelete="CASCADE"), index=True)
    normalized_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("normalized_records.id", ondelete="CASCADE"), index=True, nullable=True
    )
    source_field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    target_field: Mapped[str] = mapped_column(String(120))
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    transformed_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    transformation_type: Mapped[str] = mapped_column(String(60))
    mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_mappings.id", ondelete="SET NULL"), nullable=True
    )
    mapping_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_result: Mapped[str] = mapped_column(String(20), default="passed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecoveryOperation(Base):
    __tablename__ = "recovery_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_sources.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # mapping_correction | quarantine_retry | manual_reprocess
    trigger: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    records_reprocessed: Mapped[int] = mapped_column(Integer, default=0)
    records_recovered: Mapped[int] = mapped_column(Integer, default=0)
    records_still_failing: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebhookDelivery(Base):
    """Audit of outbound n8n webhook attempts. Failures are recorded, never raised."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20))  # delivered | failed | skipped
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
