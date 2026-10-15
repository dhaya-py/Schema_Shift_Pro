import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.integrations import n8n
from app.models import DataSource, NormalizedRecord, QuarantinedRecord, WebhookDelivery
from tests.conftest import upload, upload_sample

API = "/api/v1"


def test_dashboard_reflects_database(client, db):
    empty = client.get(f"{API}/dashboard/summary").json()
    assert empty["datasets"] == 0 and empty["normalized_records"] == 0 and empty["recent_runs"] == []

    ds = upload(client, "a.csv", "customer_id,customer_name,total_spent\nC1,Ann,1\nC2,Bob,x\n",
                source_name="S").json()["dataset"]
    client.post(f"{API}/datasets/{ds['id']}/process")
    s = client.get(f"{API}/dashboard/summary").json()
    assert (s["datasets"], s["processing_runs"], s["normalized_records"], s["quarantined_open"]) == (1, 1, 1, 1)
    assert s["normalized_records"] == db.scalar(select(func.count(NormalizedRecord.id)))
    assert s["quarantined_open"] == db.scalar(select(func.count(QuarantinedRecord.id)))
    dq = client.get(f"{API}/dashboard/data-quality").json()
    assert dq["open_quarantine_by_reason"] == [{"code": "INVALID_NUMBER", "records": 1}]
    assert dq["datasets"][0] | {"valid": 1, "quarantined": 1} == dq["datasets"][0]
    changes = client.get(f"{API}/dashboard/schema-changes").json()["reports"]
    assert len(changes) == 1 and changes[0]["dataset_id"] == ds["id"]


def _configure_webhook(monkeypatch, handler):
    monkeypatch.setenv("SCHEMA_SHIFT_N8N_WEBHOOK_URL", "https://n8n.example.test/webhook/schema-shift")
    monkeypatch.setenv("SCHEMA_SHIFT_N8N_WEBHOOK_SECRET", "test-secret")
    get_settings.cache_clear()
    n8n.set_transport(httpx.MockTransport(handler))


def test_webhook_failure_does_not_break_processing(client, db, monkeypatch):
    def boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)

    _configure_webhook(monkeypatch, boom)
    ds = upload(client, "a.csv", "customer_id,customer_name\nC1,Ann\n", source_name="S")
    assert ds.status_code == 201
    run = client.post(f"{API}/datasets/{ds.json()['dataset']['id']}/process")
    assert run.status_code == 200 and run.json()["records_valid"] == 1
    db.expire_all()
    deliveries = db.scalars(select(WebhookDelivery)).all()
    assert {d.event_type for d in deliveries} >= {"dataset.uploaded", "processing.completed"}
    assert all(d.status == "failed" and "ConnectTimeout" in d.error for d in deliveries)
    assert db.scalar(select(func.count(NormalizedRecord.id))) == 1


def test_webhook_http_error_recorded(client, db, monkeypatch):
    _configure_webhook(monkeypatch, lambda request: httpx.Response(500, text="n8n down"))
    r = client.post(f"{API}/integrations/n8n/test").json()
    assert r["status"] == "failed" and r["http_status"] == 500


def test_webhook_success_is_signed(client, db, monkeypatch):
    seen = []

    def ok(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    _configure_webhook(monkeypatch, ok)
    ds = upload(client, "a.csv", "customer_id,customer_name,total_spent\nC1,Ann,bad\n", source_name="S").json()
    client.post(f"{API}/datasets/{ds['dataset']['id']}/process")
    events = [r.headers["X-Schema-Shift-Event"] for r in seen]
    assert events == ["dataset.uploaded", "processing.completed", "quarantine.created"]
    assert all(r.headers["X-Schema-Shift-Signature"].startswith("sha256=") for r in seen)
    status = client.get(f"{API}/integrations/n8n").json()
    assert status["configured"] is True
    assert {d["status"] for d in status["recent_deliveries"]} == {"delivered"}


def test_without_webhook_url_events_are_skipped(client, db):
    ds = upload(client, "a.csv", "customer_id,customer_name\nC1,Ann\n", source_name="S").json()
    client.post(f"{API}/datasets/{ds['dataset']['id']}/process")
    db.expire_all()
    assert {d.status for d in db.scalars(select(WebhookDelivery)).all()} == {"skipped"}


def test_demo_load_is_deterministic_and_leaves_user_data(client, db):
    upload(client, "mine.csv", "customer_id,customer_name\nU1,User\n", source_name="My Source")
    first = client.post(f"{API}/demo/load").json()
    second = client.post(f"{API}/demo/load").json()
    strip = lambda r: [(s["file"], s["records"], s["valid"], s["quarantined"], s["error_counts"]) for s in r["steps"]]
    assert strip(first) == strip(second)
    assert second["removed_demo_sources"] == 4
    assert strip(first) == [
        ("crm_customers_v1.csv", 6, 6, 0, {}),
        ("shop_api_customers.json", 4, 4, 0, {}),
        ("crm_customers_v2.csv", 6, 3, 3, {"DUPLICATE_RECORD": 1, "INVALID_NUMBER": 1, "MISSING_REQUIRED_FIELD": 1}),
        ("crm_customers_v3.csv", 4, 4, 0, {}),
        ("legacy_pos_export.csv", 4, 0, 4, {"UNMAPPED_REQUIRED_FIELD": 4}),
        ("web_signups.json", 4, 0, 4, {"INVALID_NUMBER": 4}),
    ]
    db.expire_all()
    assert db.scalar(select(DataSource).where(DataSource.name == "My Source")) is not None


def test_end_to_end_upload_drift_mapping_quarantine_correction_lineage(client):
    # 1. Upload the first version of a source and process it.
    v1 = upload_sample(client, "crm_customers_v1.csv", "CRM Customers").json()
    sid = v1["source"]["id"]
    assert v1["drift"]["report"]["is_initial"]
    run1 = client.post(f"{API}/datasets/{v1['dataset']['id']}/process").json()
    assert run1["records_valid"] == 6

    # 2. A new version renames total_spent -> lifetime_value: drift + rename candidate + safe adaptation.
    v3 = upload(client, "crm_customers_v3.csv",
                (__import__("tests.conftest", fromlist=["SAMPLES"]).SAMPLES / "crm_customers_v3.csv").read_bytes(),
                source_id=sid).json()
    report = v3["drift"]["report"]
    assert [f["field"] for f in report["removed_fields"]] == ["total_spent"]
    assert {f["field"] for f in report["added_fields"]} == {"lifetime_value", "phone"}
    assert report["rename_candidates"][0]["to_field"] == "lifetime_value"
    sugg = client.get(f"{API}/datasets/{v3['dataset']['id']}/mapping-suggestions").json()
    spent = next(t for t in sugg["targets"] if t["target_field"] == "total_spent")
    assert spent["state"] == "applied_ready"
    assert spent["active_mapping"]["source_field"] == "lifetime_value"
    assert spent["active_mapping"]["evidence"]["rename_candidate"]["from_field"] == "total_spent"

    # 3. An analyst wrongly maps phone -> total_spent... approve is blocked because lifetime_value already
    #    provides total_spent in the same dataset (conflict protection).
    phone = next(m for m in client.get(f"{API}/mappings", params={"source_id": sid}).json()
                 if m["source_field"] == "phone")
    blocked = client.patch(f"{API}/mappings/{phone['id']}", json={"target_field": "total_spent"})
    assert blocked.status_code == 409

    # 4. Instead the analyst maps lifetime_value -> phone by mistake: records fail validation.
    lv = spent["active_mapping"]
    wrong = client.patch(f"{API}/mappings/{lv['id']}", json={"target_field": "phone", "mapping_status": "approved"})
    assert wrong.status_code == 409  # phone already approved for 'phone' in this dataset
    # Reject phone's own mapping first, then apply the wrong mapping.
    client.post(f"{API}/mappings/{phone['id']}/reject")
    wrong = client.patch(f"{API}/mappings/{lv['id']}", json={"target_field": "phone", "mapping_status": "approved"}).json()
    run3 = client.post(f"{API}/datasets/{v3['dataset']['id']}/process").json()
    assert run3["records_quarantined"] == 4  # "4100" is not a valid phone number
    assert run3["summary"]["error_counts"]["INVALID_FORMAT"] == 4

    # 5. Correct the mapping and reprocess affected records.
    fixed = client.post(f"{API}/mappings/{wrong['id']}/correct",
                        json={"new_target_field": "total_spent", "reason": "lifetime_value is spend"}).json()
    assert fixed["recovery"]["records_recovered"] == 4
    assert fixed["recovery"]["records_still_failing"] == 0

    # 6. Verify output and lineage.
    items = client.get(f"{API}/normalized-records", params={"dataset_id": v3["dataset"]["id"]}).json()["items"]
    by_id = {i["normalized_payload"]["customer_id"]: i for i in items}
    assert by_id["C-1011"]["normalized_payload"]["total_spent"] == 980.25
    assert by_id["C-1011"]["normalized_payload"]["phone"] is None  # phone mapping was rejected
    lin = client.get(f"{API}/normalized-records/{by_id['C-1011']['id']}/lineage").json()
    step = next(s for s in lin["steps"] if s["target_field"] == "total_spent")
    assert (step["source_field"], step["original_value"], step["mapping_version"]) == ("lifetime_value", "980.25", 3)
    assert step["mapping_method"] == "manual"
    assert len(lin["history"]) == 1  # no current output existed before; one normalized row
    summary = client.get(f"{API}/dashboard/summary").json()
    assert summary["normalized_records"] == 10 and summary["quarantined_open"] == 0
