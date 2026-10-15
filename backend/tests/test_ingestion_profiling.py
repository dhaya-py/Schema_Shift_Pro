import json

import pytest

from app.engines.profiler import classify_value, profile_records
from app.services.ingestion import IngestionError, parse_upload, sanitize_filename
from tests.conftest import upload

CSV = "customer_id,customer_name,email,total_spent\nC1,Ann,ann@example.com,10\nC2,Bob,bob@example.com,20.5\n"


# ---------- parsing ----------
def test_csv_ingestion_preserves_header_and_raw_values():
    parsed = parse_upload("a.csv", b"customer_id,Total Spent \n007, 22000 \n")
    assert parsed.field_order == ["customer_id", "Total Spent "]
    assert parsed.records == [{"customer_id": "007", "Total Spent ": " 22000 "}]
    assert any("whitespace" in w for w in parsed.warnings)


def test_json_list_and_single_object():
    parsed = parse_upload("a.json", json.dumps([{"a": 1}, {"a": 2, "b": None}]).encode())
    assert parsed.field_order == ["a", "b"]
    assert len(parsed.records) == 2
    single = parse_upload("b.json", b'{"a": 1}')
    assert single.records == [{"a": 1}]
    assert "single JSON object" in single.warnings[0]


def test_json_non_object_items_rejected_with_warning():
    parsed = parse_upload("a.json", b'[{"a": 1}, 5, "x"]')
    assert len(parsed.records) == 1
    assert "Rejected 2 item(s)" in parsed.warnings[0]


@pytest.mark.parametrize(
    "name,content,message",
    [
        ("a.txt", b"x", "Unsupported file type"),
        ("a.csv", b"", "empty"),
        ("a.json", b"[{bad json", "Invalid JSON"),
        ("a.json", b"[1, 2]", "no object records"),
        ("a.json", b"42", "array of objects"),
        ("a.csv", b"a,a\n1,2\n", "duplicate column"),
        ("a.csv", b"a,\n1,2\n", "empty column name"),
        ("a.csv", b"a,b\n", "no data rows"),
        ("a.csv", b"\xff\xfe\x00", "UTF-8"),
    ],
)
def test_invalid_uploads_rejected(name, content, message):
    with pytest.raises(IngestionError) as exc:
        parse_upload(name, content)
    assert message in str(exc.value)


def test_upload_size_limit(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("SCHEMA_SHIFT_MAX_UPLOAD_MB", "0.0001")
    get_settings.cache_clear()
    with pytest.raises(IngestionError) as exc:
        parse_upload("a.csv", CSV.encode() * 10)
    assert exc.value.status_code == 413


def test_csv_ragged_rows_are_kept_not_filled():
    parsed = parse_upload("a.csv", b"a,b\n1\n1,2,3\n\n")
    assert parsed.records[0] == {"a": "1"}  # missing cell left absent, not ""
    assert parsed.records[1]["_extra_columns"] == ["3"]
    assert any("blank" in w for w in parsed.warnings)


def test_filename_sanitized():
    assert sanitize_filename("../../etc/pa$$wd.csv") == "pa_wd.csv"
    assert sanitize_filename(None) == "upload"


# ---------- profiling ----------
def test_classify_value_types():
    assert classify_value("22000") == ("integer", "numeric")
    assert classify_value("1,250.50") == ("float", "numeric")
    assert classify_value("007")[0] == "string"  # leading zeros kept as text
    assert classify_value("unknown") == ("string", "text")
    assert classify_value("a@b.co")[1] == "email"
    assert classify_value("+1 415 555 0134")[1] == "phone"
    assert classify_value("2024-05-01")[0] == "datetime"
    assert classify_value(True)[0] == "boolean"
    assert classify_value("  ")[0] == "null"


def test_profile_mixed_type_not_inferred_from_one_value():
    records = [{"v": "10"}, {"v": "20"}, {"v": "unknown"}, {"v": ""}, {}]
    prof = profile_records(records, ["v"])["fields"]["v"]
    assert prof["inferred_type"] == "mixed"
    assert prof["dominant_type"] == "integer"
    assert prof["null_count"] == 2  # one empty + one absent key
    assert prof["present_count"] == 4
    assert prof["non_null_count"] == 3
    assert any("absent from 1 record" in o for o in prof["observations"])


def test_profile_integer_and_float_merge():
    prof = profile_records([{"v": 1}, {"v": 2.5}, {"v": "3"}], ["v"])["fields"]["v"]
    assert prof["inferred_type"] == "float"
    assert prof["distinct_count"] == 3


# ---------- schema registry via API ----------
def test_schema_version_created_and_reused(client):
    r1 = upload(client, "a.csv", CSV, source_name="CRM")
    assert r1.status_code == 201, r1.text
    sid = r1.json()["source"]["id"]
    assert r1.json()["drift"]["report"]["is_initial"] is True

    r2 = upload(client, "b.csv", CSV.replace("Ann", "Anna"), source_id=sid)
    assert r2.json()["dataset"]["schema_version_id"] == r1.json()["dataset"]["schema_version_id"]

    r3 = upload(client, "c.csv", "customer_id,customer_name,email,total_spent,phone\nC3,Cy,c@example.com,1,+1 415 555 0100\n",
                source_id=sid)
    versions = client.get(f"/api/v1/sources/{sid}/schemas").json()
    assert [v["version_number"] for v in versions] == [1, 2]
    assert r3.json()["dataset"]["schema_version_id"] == versions[1]["id"]


def test_missing_values_are_not_schema_changes(client):
    sid = upload(client, "a.csv", CSV, source_name="CRM").json()["source"]["id"]
    r = upload(client, "b.csv", "customer_id,customer_name,email,total_spent\nC9,Zed,,\n", source_id=sid)
    report = r.json()["drift"]["report"]
    assert report["added_fields"] == [] and report["removed_fields"] == []
    assert report["type_changes"] == []  # empty column is 'unknown', not a type change
    assert [c["field"] for c in report["nullability_changes"]] == ["email", "total_spent"]


def test_dataset_records_endpoint_returns_original_payload(client):
    ds = upload(client, "a.csv", CSV, source_name="CRM").json()["dataset"]
    page = client.get(f"/api/v1/datasets/{ds['id']}/records").json()
    assert page["total"] == 2
    assert page["items"][1]["original_payload"]["total_spent"] == "20.5"
    schema = client.get(f"/api/v1/datasets/{ds['id']}/schema").json()
    assert schema["fields"]["total_spent"]["inferred_type"] == "float"


def test_upload_api_errors(client):
    assert upload(client, "a.exe", "x", source_name="S").status_code == 400
    r = upload(client, "a.csv", CSV)
    assert r.status_code == 400 and "source" in r.json()["detail"].lower()
    assert upload(client, "a.csv", CSV, source_id=999).status_code == 404
