import csv, io, json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from app.pipeline import run
from app import store
from app.errors import BusinessError
from app.exporter import evidence_csv
from app.main import app

SPIKE_FILE = Path(__file__).parent.parent / "sample_data" / "soil_spike.csv"
client = TestClient(app, raise_server_exceptions=False)

@pytest.fixture
def spike(): return SPIKE_FILE.read_text(encoding="utf-8-sig")

@pytest.fixture
def spike_result(spike): return run(spike)

def resolved_row(result, sid):
    return next(r for r in result["resolved"] if r["sample_id"] == sid)

def test_spike_pair_accepted(spike_result):
    kinds = [r["kind"] for r in spike_result["normalized"]]
    assert kinds.count("spike") == 3
    assert not any(x["reason"] == "KEY_CONFLICT" for x in spike_result["quarantine"])

def test_spike_not_a_reportable_result(spike_result):
    assert len(spike_result["quality"]) == 9  # 8 样品 + 1 控制样
    assert all(r["kind"] != "spike" for r in spike_result["final"])

def test_own_spike_recovery(spike_result):
    from decimal import Decimal
    ev = spike_result["spike_evidence"]
    assert Decimal(ev["by_sample"]["S01|Pb|T001"]["recovery"]) == Decimal("0.6")
    assert ev["batch_avg"]["S01|Pb"]["n"] == 3
    assert Decimal(ev["batch_avg"]["S01|Pb"]["recovery"]) == Decimal("0.6")

def test_default_basis_and_issuable(spike_result):
    t1 = resolved_row(spike_result, "T001")
    assert (t1["basis_code"], t1["issuable_code"]) == ("OWN_SPIKE", "ISSUABLE")
    assert t1["concentration"] == "3.0000" and t1["external_concentration"] == "1.8"
    t4 = resolved_row(spike_result, "T004")
    assert (t4["basis_code"], t4["issuable_code"]) == ("BATCH_AVG", "ISSUABLE_BORROWED")
    assert t4["concentration"] == "2.5000"

def test_borrowed_basis_not_equally_reliable(spike_result):
    # 借同批均值的结论不得看起来与自身加标同样可靠
    assert resolved_row(spike_result, "T001")["issuable"] != resolved_row(spike_result, "T004")["issuable"]
    assert resolved_row(spike_result, "T004")["basis"] == "同批均值"

def test_detection_status_recomputed(spike_result):
    assert next(r for r in spike_result["quality"] if r["sample_id"] == "T007")["status"] == "BELOW_LOD"
    t7 = resolved_row(spike_result, "T007")
    assert t7["external_concentration"] == "0.048"
    assert t7["status"] == "PASS" and t7["qualifier"] == ""

def test_no_evidence_stays_external(result):
    assert all(r["basis_code"] == "EXTERNAL" and r["issuable_code"] == "HOLD" for r in result["resolved"])
    quality = {r["sample_id"]: r for r in result["quality"]}
    assert all(r["concentration"] == quality[r["sample_id"]]["concentration"] for r in result["resolved"])

def test_review_metrics(spike_result):
    m = spike_result["reports"][6]["metrics"]
    assert (m["可签发"], m["借据"], m["暂缓"]) == (3, 5, 0)

def test_orphan_spike_quarantined(spike):
    r = run(spike + "T999,spike,Pb,720,100,2,1,ug/L,S01\n")
    assert any(x["reason"] == "SPIKE_ORPHAN" for x in r["quarantine"])

def test_recovery_out_of_range_rejected(spike):
    r = run(spike + "T009,sample,Pb,320,100,0,1,ug/L,S01\nT009,spike,Pb,440,100,2,1,ug/L,S01\n")
    assert any(x["reason"] == "SPIKE_RECOVERY" for x in r["quarantine"])
    assert resolved_row(r, "T009")["basis_code"] == "BATCH_AVG"  # 自身证据无效，回落同批均值

def test_zero_added_spike_invalid(spike):
    r = run(spike + "T010,sample,Pb,320,100,0,1,ug/L,S01\nT010,spike,Pb,440,100,0,1,ug/L,S01\n")
    assert any(x["reason"] == "SPIKE_INVALID" for x in r["quarantine"])

def test_balance_with_spikes(spike_result):
    b = spike_result["balance"]
    assert b["input"] == b["accepted"] + b["quarantined"] + b["blank"]

def test_switch_basis_success(spike):
    item = store.save(spike + "\n", run(spike + "\n"))
    changed = store.switch_basis(item["id"], item["revision"], "S01|Pb|T004", "EXTERNAL")
    assert changed["revision"] == item["revision"] + 1
    t4 = resolved_row(changed["result"], "T004")
    assert (t4["basis_code"], t4["issuable_code"], t4["concentration"]) == ("EXTERNAL", "HOLD", "1.5")

def test_switch_unavailable_keeps_original(spike):
    item = store.save(spike + "\n\n", run(spike + "\n\n"))
    before = store.get(item["id"])
    with pytest.raises(BusinessError, match="无此依据"):
        store.switch_basis(item["id"], before["revision"], "S01|Pb|T004", "OWN_SPIKE")
    after = store.get(item["id"])
    assert after["revision"] == before["revision"]
    assert after["result"]["resolved"] == before["result"]["resolved"]

def test_switch_stale_revision(spike):
    item = store.save(spike + "\n\n\n", run(spike + "\n\n\n"))
    with pytest.raises(BusinessError, match="版本已更新"):
        store.switch_basis(item["id"], item["revision"] + 9, "S01|Pb|T004", "EXTERNAL")

def test_rerun_adds_no_new_conclusion(spike):
    text = spike + "\n\n\n\n"
    a = store.save(text, run(text))
    switched = store.switch_basis(a["id"], a["revision"], "S01|Pb|T004", "EXTERNAL")
    again = store.save(text, run(text))
    assert again["id"] == a["id"] and again["revision"] == switched["revision"]
    assert resolved_row(again["result"], "T004")["basis_code"] == "EXTERNAL"  # 重跑保留已切换依据
    assert len(again["result"]["resolved"]) == 8

def test_export_same_source_as_page(spike):
    item = store.save(spike, run(spike))
    rows = list(csv.reader(io.StringIO(evidence_csv(item).lstrip("\ufeff"))))
    by_key = {r["key"]: r for r in item["result"]["resolved"]}
    resolved_csv = [r for r in rows if r[3] == "resolved"]
    assert len(resolved_csv) == len(by_key) == 8
    for row in resolved_csv:
        ev = json.loads(row[6])
        assert ev["concentration"] == by_key[ev["key"]]["concentration"]
        assert ev["status"] == by_key[ev["key"]]["status"]
        assert row[5] == by_key[ev["key"]]["issuable"]

def test_export_keeps_raw_injections(spike):
    item = store.save(spike, run(spike))
    rows = list(csv.reader(io.StringIO(evidence_csv(item).lstrip("\ufeff"))))
    raw = [json.loads(r[6]) for r in rows if r[3] == "raw"]
    assert len(raw) == 21
    t1 = next(r for r in raw if r["sample_id"] == "T001" and r["kind"] == "sample")
    assert t1["area"] == "480" and t1["line"] == 12

def test_api_basis_switch(spike):
    created = client.post("/api/run", json={"text": spike + "\n\n\n\n\n"}).json()
    r = client.post("/api/basis", json={"id": created["id"], "revision": created["revision"],
                                        "key": "S01|Pb|T005", "basis": "EXTERNAL"})
    assert r.status_code == 200
    t5 = next(x for x in r.json()["result"]["resolved"] if x["sample_id"] == "T005")
    assert t5["issuable"] == "暂缓签发"

def test_api_basis_rejects_bad_payload(spike):
    created = client.post("/api/run", json={"text": spike}).json()
    assert client.post("/api/basis", json={"id": created["id"], "revision": True, "key": "k", "basis": "EXTERNAL"}).status_code == 422
    assert client.post("/api/basis", json={"id": created["id"], "revision": 0, "key": "k", "basis": "自身加标"}).status_code == 422
    r = client.post("/api/basis", json={"id": created["id"], "revision": created["revision"],
                                        "key": "S01|Pb|T005", "basis": "OWN_SPIKE"})
    assert r.status_code == 422 and r.json()["error"] == "BASIS_UNAVAILABLE"

def test_review_page_shows_basis_and_status(spike):
    created = client.post("/api/run", json={"text": spike}).json()
    r = client.get("/module/7", params={"run_id": created["id"]})
    assert r.status_code == 200
    assert "签发依据与可签发状态" in r.text and "自身加标" in r.text and "可签发·借据" in r.text
