from sqlalchemy import func, select

from app.models import FieldMapping, NormalizedRecord, QuarantinedRecord, SourceRecord, TransformationLog
from tests.conftest import upload, upload_sample

API = "/api/v1"


def _mapping(client, source_id, field):
    return next(m for m in client.get(f"{API}/mappings", params={"source_id": source_id}).json()
                if m["source_field"] == field)


def _signups_with_wrong_mapping(client):
    r = upload_sample(client, "web_signups.json", "Web Signups")
    sid, did = r.json()["source"]["id"], r.json()["dataset"]["id"]
    wrong = client.post(f"{API}/mappings", json={"source_id": sid, "source_field": "spend_band",
                                                 "target_field": "total_spent"})
    assert wrong.status_code == 201, wrong.text
    run = client.post(f"{API}/datasets/{did}/process").json()
    return sid, did, wrong.json(), run


def test_wrong_mapping_quarantines_records(client):
    _, did, _, run = _signups_with_wrong_mapping(client)
    assert (run["records_valid"], run["records_quarantined"]) == (0, 4)
    q = client.get(f"{API}/quarantine", params={"dataset_id": did}).json()
    assert q["total"] == 4
    assert all(i["errors"][0]["code"] == "INVALID_NUMBER" for i in q["items"])
    assert all(i["errors"][0]["source_field"] == "spend_band" for i in q["items"])


def test_mapping_correction_reprocesses_and_preserves_history(client, db):
    sid, did, wrong, _ = _signups_with_wrong_mapping(client)
    originals_before = [r.original_payload for r in db.scalars(select(SourceRecord).order_by(SourceRecord.id))]

    res = client.post(f"{API}/mappings/{wrong['id']}/correct",
                      json={"new_target_field": None, "replacement_source_field": "spend_total",
                            "reason": "spend_band is a category"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["corrected_mapping"]["mapping_status"] == "rejected"
    assert body["corrected_mapping"]["version"] == wrong["version"] + 1
    assert body["replacement_mapping"]["source_field"] == "spend_total"
    assert body["replacement_mapping"]["mapping_status"] == "approved"
    assert body["affected_records"] == {str(did): 4}
    rec = body["recovery"]
    assert (rec["records_reprocessed"], rec["records_recovered"], rec["records_still_failing"]) == (4, 4, 0)

    # Records were really reprocessed with the new mapping.
    items = client.get(f"{API}/normalized-records", params={"dataset_id": did}).json()["items"]
    assert sorted(i["normalized_payload"]["total_spent"] for i in items) == [65.5, 480, 720, 3150]
    # Quarantine entries resolved by a real run, not just relabelled.
    db.expire_all()
    qs = db.scalars(select(QuarantinedRecord).where(QuarantinedRecord.dataset_id == did)).all()
    assert {q.status for q in qs} == {"resolved"}
    assert all(q.resolution_run_id in rec["details"]["processing_run_ids"] for q in qs)
    # Mapping history kept; original records untouched.
    hist = client.get(f"{API}/mappings/{wrong['id']}/history").json()
    assert [(h["version"], h["mapping_status"], h["is_active"]) for h in hist] == [
        (1, "approved", False), (2, "rejected", True)]
    assert [r.original_payload for r in db.scalars(select(SourceRecord).order_by(SourceRecord.id))] == originals_before
    assert client.get(f"{API}/recovery-operations").json()[0]["trigger"] == "mapping_correction"


def test_approving_ambiguous_mapping_then_reprocessing(client):
    r = upload_sample(client, "legacy_pos_export.csv", "Legacy POS")
    sid, did = r.json()["source"]["id"], r.json()["dataset"]["id"]
    run = client.post(f"{API}/datasets/{did}/process").json()
    assert run["records_quarantined"] == 4
    issue = run["summary"]["schema_issues"][0]
    assert issue["code"] == "UNMAPPED_REQUIRED_FIELD" and issue["pending_mapping_ids"]

    approved = client.post(f"{API}/mappings/{issue['pending_mapping_ids'][0]}/approve").json()
    assert approved["mapping_status"] == "approved" and approved["version"] == 2
    op = client.post(f"{API}/reprocessing", json={"source_id": sid, "mode": "quarantined"}).json()
    assert (op["records_reprocessed"], op["records_recovered"], op["records_still_failing"]) == (4, 4, 0)
    assert client.get(f"{API}/datasets/{did}").json()["processing_status"] == "processed"


def test_retries_are_idempotent(client, db):
    ds = upload(client, "a.csv", "customer_id,customer_name\nC1,Ann\nC2,Bob\n", source_name="S").json()["dataset"]
    for _ in range(3):
        client.post(f"{API}/datasets/{ds['id']}/process")
    client.post(f"{API}/reprocessing", json={"dataset_id": ds["id"], "mode": "all"})
    current = db.scalar(select(func.count(NormalizedRecord.id)).where(NormalizedRecord.is_current.is_(True)))
    total = db.scalar(select(func.count(NormalizedRecord.id)))
    assert current == 2  # one current output per source record
    assert total == 8  # older outputs kept as superseded history


def test_failed_retry_supersedes_old_quarantine_entry(client, db):
    ds = upload(client, "a.csv", "customer_id,customer_name,total_spent\nC1,Ann,unknown\n",
                source_name="S").json()["dataset"]
    client.post(f"{API}/datasets/{ds['id']}/process")
    qid = client.get(f"{API}/quarantine").json()["items"][0]["id"]
    op = client.post(f"{API}/quarantine/{qid}/retry").json()
    assert op["records_still_failing"] == 1
    detail = client.get(f"{API}/quarantine/{qid}").json()
    assert detail["status"] == "superseded"
    assert [h["status"] for h in detail["history"]] == ["superseded", "open"]
    assert client.post(f"{API}/quarantine/{qid}/retry").status_code == 409
    assert db.scalar(select(func.count(QuarantinedRecord.id)).where(QuarantinedRecord.status == "open")) == 1


def test_lineage_answers_where_value_came_from(client):
    r = upload_sample(client, "shop_api_customers.json", "Shop API")
    did = r.json()["dataset"]["id"]
    client.post(f"{API}/datasets/{did}/process")
    items = client.get(f"{API}/normalized-records", params={"dataset_id": did}).json()["items"]
    target = next(i for i in items if i["normalized_payload"]["customer_id"] == "2004")
    lin = client.get(f"{API}/normalized-records/{target['id']}/lineage").json()
    assert lin["source_record"]["original_payload"]["customerId"] == 2004
    step = next(s for s in lin["steps"] if s["target_field"] == "customer_id")
    assert step["source_field"] == "customerId"
    assert step["original_value"] == "2004" and step["transformed_value"] == "2004"
    assert step["transformation_type"] == "rename+number_to_string"
    assert step["mapping_method"] == "normalized_name" and step["mapping_version"] == 1
    assert step["validation_result"] == "passed"
    assert lin["dataset"]["filename"] == "shop_api_customers.json"


def test_correction_reprocesses_records_produced_by_old_mapping(client, db):
    """A wrong mapping that passes validation still produces wrong output;
    correcting it must reprocess those normalized records too."""
    r = upload(client, "a.csv", "customer_id,customer_name,nickname\nC1,Ann Lee,annie\n", source_name="S")
    sid, did = r.json()["source"]["id"], r.json()["dataset"]["id"]
    name_map = _mapping(client, sid, "customer_name")
    client.patch(f"{API}/mappings/{name_map['id']}", json={"target_field": None, "mapping_status": "rejected"})
    wrong = client.post(f"{API}/mappings", json={"source_id": sid, "source_field": "nickname",
                                                 "target_field": "customer_name"}).json()
    client.post(f"{API}/datasets/{did}/process")
    res = client.post(f"{API}/mappings/{wrong['id']}/correct",
                      json={"new_target_field": None, "replacement_source_field": "customer_name"}).json()
    assert res["affected_records"] == {str(did): 1}
    item = client.get(f"{API}/normalized-records", params={"dataset_id": did}).json()["items"][0]
    assert item["normalized_payload"]["customer_name"] == "Ann Lee"
    db.expire_all()
    logs = db.scalars(select(TransformationLog).where(TransformationLog.target_field == "customer_name")
                      .order_by(TransformationLog.id)).all()
    assert [log.source_field for log in logs] == ["nickname", "customer_name"]  # old lineage kept
    versions = db.scalars(select(FieldMapping.version).where(FieldMapping.source_field == "customer_name")
                          .order_by(FieldMapping.version)).all()
    assert versions == [1, 2, 3]
