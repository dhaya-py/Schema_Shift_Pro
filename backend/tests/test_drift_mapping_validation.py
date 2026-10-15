from app.engines.drift import compare_schemas
from app.engines.mapping import score_candidates, suggest_for_dataset
from app.engines.normalization import ResolvedMapping, normalize_record
from app.engines.profiler import profile_records
from app.engines.validation import MISSING, check_field, duplicate_status
from app.services.canonical import CUSTOMER_SCHEMA_V1 as CANON
from tests.conftest import upload

FIELDS = {f["name"]: f for f in CANON["fields"]}


def _profile(rows):
    order = []
    for r in rows:
        for k in r:
            if k not in order:
                order.append(k)
    return profile_records(rows, order)


def _compare(prev_rows, cur_rows, mapped=None):
    return compare_schemas(_profile(prev_rows), _profile(cur_rows), mapped or {}, {"customer_id", "customer_name"},
                           lambda n: {"total_spent": "total_spent", "lifetime_value": "total_spent"}.get(n))


# ---------- drift ----------
def test_added_and_removed_fields_detected():
    rep = _compare([{"a": "1", "b": "x"}], [{"a": "2", "c": "y"}])
    assert [f["field"] for f in rep["added_fields"]] == ["c"]
    assert [f["field"] for f in rep["removed_fields"]] == ["b"]
    assert rep["severity"] == "info"


def test_removed_required_mapping_is_breaking():
    rep = _compare([{"cid": "1", "n": "x"}], [{"n": "y"}], mapped={"cid": "customer_id"})
    assert rep["removed_fields"][0]["severity"] == "breaking"
    assert rep["severity"] == "breaking"


def test_type_change_detected():
    rep = _compare([{"v": "1"}, {"v": "2"}], [{"v": "abc"}, {"v": "def"}], mapped={"v": "total_spent"})
    change = rep["type_changes"][0]
    assert (change["previous_type"], change["current_type"], change["compatibility"]) == ("integer", "string", "incompatible")
    assert change["severity"] == "breaking"


def test_widening_is_info_and_mixed_reported_once():
    widen = _compare([{"v": "1"}], [{"v": "1.5"}])
    assert widen["type_changes"][0]["compatibility"] == "widening"
    assert widen["severity"] == "info"
    mixed = _compare([{"v": "1"}, {"v": "2"}], [{"v": "3"}, {"v": "unknown"}])
    assert mixed["type_changes"] == []
    assert mixed["mixed_type_fields"][0]["field"] == "v"


def test_rename_candidate_is_a_candidate_with_evidence():
    prev = [{"customer_id": "1", "total_spent": "10"}, {"customer_id": "2", "total_spent": "20"}]
    cur = [{"customer_id": "3", "lifetime_value": "30"}, {"customer_id": "4", "lifetime_value": "40"}]
    rep = _compare(prev, cur)
    cand = rep["rename_candidates"][0]
    assert (cand["from_field"], cand["to_field"], cand["status"]) == ("total_spent", "lifetime_value", "candidate")
    assert cand["components"]["shared_canonical_target"] == 1.0
    assert cand["score"] >= 0.55


def test_unrelated_fields_not_rename_candidates():
    rep = _compare([{"age": "31"}, {"age": "40"}], [{"zip_code": "abc"}, {"zip_code": "def"}])
    assert rep["rename_candidates"] == []


# ---------- mapping ----------
def test_mapping_methods_and_confidence():
    def best(field, values):
        prof = _profile([{field: v} for v in values])["fields"][field]
        return score_candidates(field, prof, values, CANON)[0]

    assert (best("customer_id", ["C1"])["method"], best("customer_id", ["C1"])["confidence"]) == ("exact", 1.0)
    assert best("customerId", ["C1"])["method"] == "normalized_name"
    assert best("lifetime_value", ["10"])["method"] == "alias"
    sim = best("spend_total", ["10.00", "20.50"])
    assert sim["method"] == "similarity" and sim["target_field"] == "total_spent" and sim["confidence"] <= 0.85


def test_ambiguous_id_is_not_auto_approved():
    rows = [{"id": "1001", "name": "Ann"}, {"id": "1002", "name": "Bob"}]
    prof = _profile(rows)
    values = {k: [r[k] for r in rows] for k in prof["field_order"]}
    props = {p["source_field"]: p for p in suggest_for_dataset(prof, values, CANON, {})}
    assert props["id"]["target_field"] == "customer_id"
    assert props["id"]["ambiguous"] is True
    assert props["id"]["confidence"] == 0.5
    assert props["id"]["auto_approve"] is False
    assert props["name"]["auto_approve"] is True


def test_competing_fields_need_review():
    rows = [{"cust_id": "C1", "customerId": "C1"}]
    prof = _profile(rows)
    values = {k: [r[k] for r in rows] for k in prof["field_order"]}
    props = suggest_for_dataset(prof, values, CANON, {})
    assert all(not p["auto_approve"] for p in props if p["target_field"] == "customer_id")


def test_low_value_pass_rate_blocks_auto_approval():
    rows = [{"lifetime_value": v} for v in ["low", "high", "medium", "10"]]
    prof = _profile(rows)
    props = suggest_for_dataset(prof, {"lifetime_value": [r["lifetime_value"] for r in rows]}, CANON, {})
    assert props[0]["auto_approve"] is False
    assert "validation" in props[0]["policy_reason"]


# ---------- validation + normalization ----------
def test_invalid_numeric_is_never_zero():
    res = check_field(FIELDS["total_spent"], "unknown")
    assert not res.ok and res.value is None
    assert res.errors[0]["code"] == "INVALID_NUMBER"
    assert "not converted to 0" in res.errors[0]["message"]


def test_numeric_string_normalized_safely():
    assert check_field(FIELDS["total_spent"], "22000").value == 22000
    assert check_field(FIELDS["total_spent"], " 1,250.50 ").value == 1250.5
    assert check_field(FIELDS["total_spent"], "-5").errors[0]["code"] == "OUT_OF_RANGE"
    assert check_field(FIELDS["total_spent"], True).errors[0]["code"] == "INVALID_TYPE"


def test_missing_required_and_optional_policies():
    assert check_field(FIELDS["customer_id"], "").errors[0]["code"] == "MISSING_REQUIRED_FIELD"
    assert check_field(FIELDS["customer_id"], MISSING).errors[0]["code"] == "MISSING_REQUIRED_FIELD"
    opt = check_field(FIELDS["phone"], "")
    assert opt.ok and opt.value is None and opt.transformations == ["empty_to_null"]


def test_identifier_keeps_leading_zeros_and_formats():
    assert check_field(FIELDS["customer_id"], "00731").value == "00731"
    assert check_field(FIELDS["customer_id"], 2004).value == "2004"
    assert check_field(FIELDS["customer_id"], 1.5).errors[0]["code"] == "INVALID_TYPE"
    assert check_field(FIELDS["email"], "bad@").errors[0]["code"] == "INVALID_FORMAT"
    assert check_field(FIELDS["email"], "A@Example.COM").value == "a@example.com"
    assert check_field(FIELDS["phone"], "12").errors[0]["code"] == "INVALID_FORMAT"


def test_normalize_record_renames_and_logs_transformations():
    maps = {
        "customer_id": ResolvedMapping(1, 1, "cust_id", "customer_id", "alias"),
        "customer_name": ResolvedMapping(2, 1, "full_name", "customer_name", "alias"),
        "total_spent": ResolvedMapping(3, 2, "lifetime_value", "total_spent", "alias"),
    }
    out = normalize_record({"cust_id": "C1", "full_name": " Ann  Lee ", "lifetime_value": "22000"}, CANON, maps)
    assert out.ok
    assert out.normalized == {"customer_id": "C1", "customer_name": "Ann Lee", "email": None,
                              "total_spent": 22000, "phone": None}
    spent = next(s for s in out.lineage if s["target_field"] == "total_spent")
    assert spent["transformation_type"] == "rename+string_to_number"
    assert (spent["original_value"], spent["transformed_value"], spent["mapping_version"]) == ("22000", "22000", 2)


def test_unmapped_required_field_is_schema_level_error():
    out = normalize_record({"x": "1"}, CANON, {})
    codes = {e["code"] for e in out.errors}
    assert codes == {"UNMAPPED_REQUIRED_FIELD"}
    assert all(e["level"] == "schema" for e in out.errors)


def test_duplicate_status():
    seen = {"C1": {"payload": {"a": 1}, "record_index": 0}}
    assert duplicate_status("C1", {"a": 1}, seen)[0] == "DUPLICATE_RECORD"
    assert duplicate_status("C1", {"a": 2}, seen)[0] == "CONFLICTING_RECORD"
    assert duplicate_status("C2", {"a": 1}, seen)[0] is None


# ---------- processing via API ----------
def test_partial_quarantine_keeps_valid_records(client):
    csv = ("customer_id,customer_name,total_spent\n"
           "C1,Ann,10\nC2,Bob,unknown\n,Cy,5\nC3,Di,7\nC3,Di,7\nC4,Ed,\" 22000 \"\n")
    ds = upload(client, "a.csv", csv, source_name="S").json()["dataset"]
    run = client.post(f"/api/v1/datasets/{ds['id']}/process").json()
    assert (run["records_processed"], run["records_valid"], run["records_quarantined"]) == (6, 3, 3)
    assert run["status"] == "completed_with_quarantine"
    assert run["summary"]["error_counts"] == {"DUPLICATE_RECORD": 1, "INVALID_NUMBER": 1, "MISSING_REQUIRED_FIELD": 1}
    q = client.get("/api/v1/quarantine", params={"dataset_id": ds["id"]}).json()
    bad = next(i for i in q["items"] if i["errors"][0]["code"] == "INVALID_NUMBER")
    assert bad["original_payload"]["total_spent"] == "unknown"  # original preserved
    normalized = client.get("/api/v1/normalized-records", params={"dataset_id": ds["id"]}).json()["items"]
    assert sorted(n["normalized_payload"]["total_spent"] for n in normalized) == [7, 10, 22000]


def test_conflicting_mapping_rejected_on_approve(client):
    ds = upload(client, "a.csv", "cust_id,customerId,customer_name\nC1,C1,Ann\n", source_name="S").json()["dataset"]
    sugg = client.get(f"/api/v1/datasets/{ds['id']}/mapping-suggestions").json()
    target = next(t for t in sugg["targets"] if t["target_field"] == "customer_id")
    assert target["state"] == "needs_review" and len(target["candidates"]) == 2
    ok = client.post(f"/api/v1/mappings/{target['candidates'][0]['id']}/approve")
    assert ok.status_code == 200
    clash = client.post(f"/api/v1/mappings/{target['candidates'][1]['id']}/approve")
    assert clash.status_code == 409


def test_pending_review_quarantines_instead_of_nulling(client):
    csv = "customer_id,customer_name,spend_total\nC1,Ann,10\nC2,Bob,\n"
    r = upload(client, "a.csv", csv, source_name="S").json()
    did, sid = r["dataset"]["id"], r["source"]["id"]
    run = client.post(f"/api/v1/datasets/{did}/process").json()
    assert (run["records_valid"], run["records_quarantined"]) == (1, 1)
    assert run["summary"]["error_counts"] == {"MAPPING_PENDING_REVIEW": 1}
    issue = next(i for i in run["summary"]["schema_issues"] if i["code"] == "MAPPING_PENDING_REVIEW")
    client.post(f"/api/v1/mappings/{issue['pending_mapping_ids'][0]}/approve")
    op = client.post("/api/v1/reprocessing", json={"source_id": sid, "mode": "quarantined"}).json()
    assert op["records_recovered"] == 1
    values = sorted((i["normalized_payload"]["customer_id"], i["normalized_payload"]["total_spent"])
                    for i in client.get("/api/v1/normalized-records").json()["items"])
    assert values == [("C1", 10), ("C2", None)]
