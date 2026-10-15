"""Pydantic request/response schemas for the REST API."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- Sources ----------
class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    source_type: str = Field(default="file", max_length=40)
    description: str | None = Field(default=None, max_length=1000)


class SourceOut(ORM):
    id: int
    name: str
    source_type: str
    description: str | None
    is_demo: bool
    created_at: datetime


class SchemaVersionOut(ORM):
    id: int
    source_id: int
    version_number: int
    schema_definition: dict
    detected_at: datetime
    change_summary: str


class SourceDetail(SourceOut):
    schema_versions: list[SchemaVersionOut]
    dataset_count: int
    latest_version: int | None


# ---------- Datasets ----------
class DatasetOut(ORM):
    id: int
    source_id: int
    schema_version_id: int | None
    filename: str
    format: str
    uploaded_at: datetime
    record_count: int
    processing_status: str
    ingestion_warnings: list[str]


class DriftReportOut(ORM):
    id: int
    source_id: int
    dataset_id: int
    previous_version_id: int | None
    current_version_id: int | None
    severity: str
    change_count: int
    report: dict
    created_at: datetime


class DatasetDetail(DatasetOut):
    source_name: str
    schema_version_number: int | None
    profile: dict
    drift: DriftReportOut | None
    open_quarantine: int
    current_normalized: int


class UploadResult(BaseModel):
    dataset: DatasetOut
    source: SourceOut
    drift: DriftReportOut
    mappings_created: int
    mappings_pending_review: int


class SourceRecordOut(ORM):
    id: int
    dataset_id: int
    record_index: int
    original_payload: dict
    payload_hash: str
    created_at: datetime


# ---------- Mappings ----------
class MappingOut(ORM):
    id: int
    source_id: int
    source_field: str
    target_field: str | None
    mapping_status: str
    confidence: float | None
    mapping_method: str
    version: int
    is_active: bool
    decided_by: str | None
    evidence: dict
    previous_mapping_id: int | None
    created_at: datetime
    updated_at: datetime


class MappingCreate(BaseModel):
    source_id: int
    source_field: str = Field(min_length=1, max_length=255)
    target_field: str = Field(min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=500)


class MappingUpdate(BaseModel):
    target_field: str | None = None
    mapping_status: Literal["approved", "rejected", "suggested"] | None = None
    note: str | None = Field(default=None, max_length=500)


class MappingCorrection(BaseModel):
    new_target_field: str | None = Field(
        default=None, description="New target for this source field, or null to stop mapping it."
    )
    replacement_source_field: str | None = Field(
        default=None, description="Another source field that should provide the old target instead."
    )
    reason: str | None = Field(default=None, max_length=500)
    reprocess: bool = True


class CanonicalFieldOut(BaseModel):
    name: str
    type: str
    required: bool


class MappingSuggestionTarget(BaseModel):
    target_field: str
    target_type: str
    required: bool
    active_mapping: MappingOut | None
    candidates: list[MappingOut]
    state: Literal["applied_ready", "needs_review", "conflict", "unmapped"]


class MappingSuggestions(BaseModel):
    dataset_id: int
    source_id: int
    targets: list[MappingSuggestionTarget]
    unmapped_source_fields: list[str]
    canonical_fields: list[CanonicalFieldOut]


# ---------- Processing ----------
class RunOut(ORM):
    id: int
    dataset_id: int
    status: str
    trigger: str
    recovery_operation_id: int | None
    started_at: datetime
    completed_at: datetime | None
    records_processed: int
    records_valid: int
    records_quarantined: int
    summary: dict


class NormalizedOut(ORM):
    id: int
    processing_run_id: int
    source_record_id: int
    dataset_id: int
    normalized_payload: dict
    validation_status: str
    is_current: bool
    superseded_by_run_id: int | None
    created_at: datetime


class LineageStep(ORM):
    id: int
    source_field: str | None
    target_field: str
    original_value: str | None
    transformed_value: str | None
    transformation_type: str
    mapping_id: int | None
    mapping_version: int | None
    mapping_method: str | None = None
    mapping_decided_by: str | None = None
    validation_result: str
    created_at: datetime


class Lineage(BaseModel):
    normalized_record: NormalizedOut
    source_record: SourceRecordOut
    dataset: DatasetOut
    source: SourceOut
    processing_run: RunOut
    steps: list[LineageStep]
    history: list[NormalizedOut]


class QuarantineOut(ORM):
    id: int
    processing_run_id: int
    source_record_id: int
    dataset_id: int
    failure_reason: str
    errors: list[dict[str, Any]]
    original_payload: dict
    status: str
    resolution_run_id: int | None
    resolution_note: str | None
    created_at: datetime
    resolved_at: datetime | None


class QuarantineDetail(QuarantineOut):
    record_index: int
    dataset_filename: str
    source_id: int
    history: list[QuarantineOut]


class RecoveryOut(ORM):
    id: int
    source_id: int | None
    trigger: str
    description: str
    details: dict
    records_reprocessed: int
    records_recovered: int
    records_still_failing: int
    created_at: datetime


class ReprocessRequest(BaseModel):
    source_id: int | None = None
    dataset_id: int | None = None
    mode: Literal["quarantined", "all"] = "quarantined"


class CorrectionResult(BaseModel):
    corrected_mapping: MappingOut
    replacement_mapping: MappingOut | None
    affected_records: dict[str, int]
    recovery: RecoveryOut | None


class WebhookDeliveryOut(ORM):
    id: int
    event_type: str
    status: str
    http_status: int | None
    error: str | None
    attempted_at: datetime


class Page(BaseModel):
    total: int
    items: list[Any]
