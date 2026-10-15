"""Ingestion service: parse uploads, store original records, profile the schema,
register schema versions, record drift and create mapping suggestions."""
import csv
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.engines.drift import compare_schemas
from app.engines.mapping import resolve_by_name
from app.engines.profiler import profile_records, schema_signature
from app.models import DataSource, Dataset, DriftReport, FieldMapping, SchemaVersion, SourceRecord
from app.services import mappings as mapping_service
from app.services.canonical import get_canonical, required_targets
from app.utils.text import stable_hash

ALLOWED_EXTENSIONS = {".csv": "csv", ".json": "json"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")


class IngestionError(ValueError):
    """Raised for user-correctable upload problems (mapped to HTTP 400/413)."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ParsedUpload:
    format: str
    records: list[dict]
    field_order: list[str]
    warnings: list[str] = field(default_factory=list)


def sanitize_filename(filename: str | None) -> str:
    base = PurePath((filename or "").replace("\\", "/")).name
    cleaned = _SAFE_NAME_RE.sub("_", base).strip(" .")
    return cleaned[:200] or "upload"


def detect_format(filename: str) -> str:
    ext = PurePath(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise IngestionError(f"Unsupported file type '{ext or 'none'}'. Upload a .csv or .json file.")
    return ALLOWED_EXTENSIONS[ext]


def _decode(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestionError(f"File is not valid UTF-8 text (byte {exc.start}).") from exc


def parse_csv(text: str) -> ParsedUpload:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise IngestionError(f"CSV could not be parsed: {exc}") from exc
    rows_iter = [(i + 1, r) for i, r in enumerate(rows)]
    while rows_iter and not any(c.strip() for c in rows_iter[0][1]):
        rows_iter.pop(0)
    if not rows_iter:
        raise IngestionError("CSV file has no header row.")
    header_line, header = rows_iter[0]
    if any(h.strip() == "" for h in header):
        raise IngestionError(f"CSV header on line {header_line} has an empty column name.")
    dupes = sorted({h for h in header if header.count(h) > 1})
    if dupes:
        raise IngestionError(f"CSV header has duplicate column names: {', '.join(dupes)}.")
    warnings: list[str] = []
    if any(h != h.strip() for h in header):
        warnings.append("Some column names have surrounding whitespace; names were kept exactly as provided.")

    records: list[dict] = []
    blank = short = long = 0
    for line_no, row in rows_iter[1:]:
        if not any(c.strip() for c in row):
            blank += 1
            continue
        rec = {h: row[i] for i, h in enumerate(header) if i < len(row)}
        if len(row) < len(header):
            short += 1
        elif len(row) > len(header):
            long += 1
            rec["_extra_columns"] = row[len(header):]
        records.append(rec)
    if blank:
        warnings.append(f"Skipped {blank} blank row(s).")
    if short:
        warnings.append(f"{short} row(s) had fewer cells than the header; missing cells were left absent, not filled.")
    if long:
        warnings.append(f"{long} row(s) had extra cells; they were kept under '_extra_columns'.")
    if not records:
        raise IngestionError("CSV file has a header but no data rows.")
    order = list(header) + (["_extra_columns"] if long else [])
    return ParsedUpload("csv", records, order, warnings)


def parse_json(text: str) -> ParsedUpload:
    if not text.strip():
        raise IngestionError("JSON file is empty.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestionError(f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}.") from exc
    warnings: list[str] = []
    if isinstance(data, dict):
        data = [data]
        warnings.append("File contained a single JSON object; it was ingested as one record.")
    if not isinstance(data, list):
        raise IngestionError("JSON must be an array of objects or a single object.")
    records, rejected = [], []
    for i, item in enumerate(data):
        if isinstance(item, dict):
            records.append(item)
        else:
            rejected.append(i)
    if rejected:
        shown = ", ".join(str(i) for i in rejected[:10])
        warnings.append(f"Rejected {len(rejected)} item(s) that are not objects (array positions {shown}).")
    if not records:
        raise IngestionError("JSON contains no object records.")
    order: list[str] = []
    seen: set[str] = set()
    for rec in records:
        for key in rec:
            if key not in seen:
                seen.add(key)
                order.append(key)
    return ParsedUpload("json", records, order, warnings)


def parse_upload(filename: str, content: bytes) -> ParsedUpload:
    settings = get_settings()
    fmt = detect_format(filename)
    if len(content) == 0:
        raise IngestionError("Uploaded file is empty.")
    if len(content) > settings.max_upload_bytes:
        raise IngestionError(
            f"File is {len(content) / 1_048_576:.1f} MB; the limit is {settings.max_upload_mb:g} MB.", status_code=413
        )
    text = _decode(content)
    parsed = parse_csv(text) if fmt == "csv" else parse_json(text)
    if len(parsed.records) > settings.max_records_per_upload:
        raise IngestionError(
            f"File has {len(parsed.records)} records; the limit is {settings.max_records_per_upload}.", status_code=413
        )
    return parsed


def get_or_create_source(db: Session, source_id: int | None, source_name: str | None,
                         source_type: str = "file", description: str | None = None, is_demo: bool = False) -> DataSource:
    if source_id is not None:
        source = db.get(DataSource, source_id)
        if not source:
            raise IngestionError(f"Source {source_id} does not exist.", status_code=404)
        return source
    name = (source_name or "").strip()
    if not name:
        raise IngestionError("Choose an existing source or enter a name for a new one.")
    if len(name) > 120:
        raise IngestionError("Source name must be 120 characters or fewer.")
    source = db.scalar(select(DataSource).where(DataSource.name == name))
    if source:
        return source
    source = DataSource(name=name, source_type=source_type, description=description, is_demo=is_demo)
    db.add(source)
    db.flush()
    return source


def _approved_target_map(db: Session, source_id: int) -> dict[str, str]:
    rows = db.scalars(
        select(FieldMapping).where(
            FieldMapping.source_id == source_id,
            FieldMapping.is_active.is_(True),
            FieldMapping.mapping_status == "approved",
        )
    ).all()
    return {m.source_field: m.target_field for m in rows if m.target_field}


def ingest_dataset(db: Session, source: DataSource, filename: str, parsed: ParsedUpload) -> tuple[Dataset, DriftReport]:
    """Store records and run profiling, schema registry, drift detection and
    mapping suggestion. The caller commits."""
    canonical = get_canonical(db).schema_definition
    profile = profile_records(parsed.records, parsed.field_order)

    dataset = Dataset(
        source_id=source.id,
        filename=filename,
        format=parsed.format,
        record_count=len(parsed.records),
        processing_status="uploaded",
        profile=profile,
        ingestion_warnings=parsed.warnings,
    )
    db.add(dataset)
    db.flush()
    db.add_all(
        SourceRecord(dataset_id=dataset.id, record_index=i, original_payload=rec, payload_hash=stable_hash(rec))
        for i, rec in enumerate(parsed.records)
    )

    # Schema registry: new version only when field names or inferred types change.
    latest = db.scalar(
        select(SchemaVersion)
        .where(SchemaVersion.source_id == source.id)
        .order_by(SchemaVersion.version_number.desc())
    )
    mapped = _approved_target_map(db, source.id)

    def resolve_target(name: str) -> str | None:
        if name in mapped:
            return mapped[name]
        hit = resolve_by_name(name, canonical)
        return hit[0] if hit and not hit[2] else None

    report = compare_schemas(
        latest.schema_definition if latest else None, profile, mapped, required_targets(canonical), resolve_target
    )
    if latest and schema_signature(latest.schema_definition) == schema_signature(profile):
        current = latest
        report["summary"] = f"Matches schema version {latest.version_number}. " + (
            report["summary"] if report["change_count"] else "No structural changes."
        )
    else:
        current = SchemaVersion(
            source_id=source.id,
            version_number=(latest.version_number + 1) if latest else 1,
            schema_definition=profile,
            change_summary=report["summary"],
        )
        db.add(current)
        db.flush()
    dataset.schema_version_id = current.id
    drift = DriftReport(
        source_id=source.id,
        dataset_id=dataset.id,
        previous_version_id=latest.id if latest else None,
        current_version_id=current.id,
        severity=report["severity"],
        change_count=report["change_count"],
        report=report,
    )
    db.add(drift)
    db.flush()

    values_by_field = {name: [r[name] for r in parsed.records if name in r] for name in parsed.field_order}
    mapping_service.create_suggestions(db, source, dataset, profile, values_by_field, canonical, report)
    dataset.processing_status = "profiled"
    return dataset, drift
