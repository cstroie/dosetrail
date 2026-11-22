"""API behaviour on a small committed SQLite database."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dosimetrix import classify
from dosimetrix.api.app import create_app
from dosimetrix.config import Settings
from dosimetrix.db.migrate import upgrade
from dosimetrix.db.models import AuditLog, Device, Event, Patient, Study, User
from dosimetrix.db.session import make_engine, make_sessionmaker
from dosimetrix.security import hash_password
from dosimetrix.seeds.loader import load_all

SECRET = b"s" * 32
NOW = dt.datetime(2026, 11, 21, 10, 0)


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    url = f"sqlite:///{tmp_path_factory.mktemp('api') / 'api.sqlite3'}"
    upgrade(url)
    engine = make_engine(url)
    with make_sessionmaker(engine)() as s:
        s.add_all([
            User(username="viewer", password_hash=hash_password("viewer-pass-1"), roles="viewer"),
            User(username="phys", password_hash=hash_password("phys-pass-12"), roles="viewer,pid"),
            User(username="boss", password_hash=hash_password("boss-pass-123"),
                 roles="viewer,admin"),
        ])
        dev = Device(display_name="Angio Suite", model_name="Azurion", identity_key="k1",
                     is_local=True, n_studies=1, last_study_at=NOW - dt.timedelta(days=5))
        pat = Patient(patient_id="2041187", name="POPESCU^ION", pseudo_id="P-ABCDE-12345",
                      birth_date=dt.date(1959, 3, 1), sex="M")
        s.add_all([dev, pat])
        s.flush()
        st = Study(study_uid="1.2.3", accession="A1", modality="XA", device_id=dev.id,
                   patient_id=pat.id, study_datetime=NOW - dt.timedelta(days=5),
                   study_description="Coronary angiography", age_days=24500, weight_kg=88,
                   total_dap=12.5, total_rp_dose=0.42, n_events=1, source_kind="test")
        st.events.append(Event(event_uid="e1", event_type="fluoro", rp_dose=0.123,
                               dap=1.5, field_w_mm=200, sid_mm=1050, fluoro_time_s=30,
                               event_datetime=NOW - dt.timedelta(days=5)))
        s.add(st)
        load_all(s)
        rx = Study(study_uid="1.2.4", accession="A2", modality="DX", device_id=dev.id,
                   study_datetime=NOW - dt.timedelta(days=2), study_description="Spine",
                   n_events=2, source_kind="test")
        rx.events = [Event(protocol="COLOANA", anatomy="Spine", dap=0.2),
                     Event(protocol="W035 Torace a.p. 10kg", view="antero-posterior", dap=0.05)]
        s.add(rx)
        s.flush()
        classify.run(s)
        s.commit()
    app = create_app(Settings(database_url=url), engine=engine, secret=SECRET, now=NOW)
    return TestClient(app)


def login(client, user, pw):
    client.cookies.clear()
    r = client.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def test_requires_login(client):
    client.cookies.clear()
    assert client.get("/api/studies").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/api/login", json={"username": "viewer", "password": "x"}
                       ).status_code == 401


def test_cookie_flags(client):
    r = login(client, "viewer", "viewer-pass-1")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie  # plain HTTP on the LAN still logs in


def test_cookie_is_secure_over_https(client):
    https = TestClient(client.app, base_url="https://testserver")
    r = https.post("/api/login", json={"username": "viewer", "password": "viewer-pass-1"})
    assert r.status_code == 200 and "secure" in r.headers["set-cookie"].lower()


def test_studies_carry_pseudonyms_only(client):
    login(client, "viewer", "viewer-pass-1")
    body = client.get("/api/studies", params={"q": "modality:XA"}).text
    assert "P-ABCDE-12345" in body
    assert "POPESCU" not in body and "2041187" not in body and "1959" not in body
    res = client.get("/api/studies", params={"q": "pid:P-ABCDE-12345"}).json()
    assert res["total"] == 1 and res["rows"][0]["patient"]["age"] == 67


def test_detail_units(client):
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    d = client.get(f"/api/studies/{sid}").json()
    ev = d["events"][0]
    assert ev["kar_mgy"] == 123.0 and ev["field_cm"] == 20.0 and ev["sid_cm"] == 105.0
    assert d["thresholds"]["source"] == "default"
    assert "identified" not in d["patient"] and "POPESCU" not in str(d)


def test_identify_needs_pid_role_and_is_audited(client):
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    assert client.post(f"/api/studies/{sid}/identify").status_code == 403
    login(client, "phys", "phys-pass-12")
    r = client.post(f"/api/studies/{sid}/identify").json()
    assert r["name"] == "POPESCU^ION" and r["id"] == "2041187"


def test_audit_entry_written(client, tmp_path_factory):
    login(client, "phys", "phys-pass-12")
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    client.post(f"/api/studies/{sid}/identify")
    # read the audit log through a fresh engine on the same file
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        actions = [a.action for a in s.scalars(select(AuditLog))]
    assert "identify" in actions and "login" in actions and "login_failed" in actions


def test_overview_and_pages(client):
    login(client, "viewer", "viewer-pass-1")
    ov = client.get("/api/overview").json()
    assert [m["code"] for m in ov["modalities"]] == ["CT", "DX", "RF", "MG"]
    assert ov["feeds"][0]["status"] == "stale"  # last study 5 days ago
    page = client.get("/studies.html")
    assert page.status_code == 200 and 'data-api="http://testserver"' in page.text
    assert client.get("/assets/css/tokens.css").status_code == 200
    assert client.get("/api/lookup", params={"q": "A1"}).json() == []  # min 3 chars
    assert client.get("/api/lookup", params={"q": "P-ABC"}).json() == []  # too short
    assert client.get("/api/lookup", params={"q": "P-ABCDE"}).json()[0]["accession"] == "A1"


def _add_device_with_study(client, name, serial, uid, modality="DX"):
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        d = Device(display_name=name, model_name="Ysio", manufacturer="SIEMENS",
                   serial_number=serial, identity_key=f"k-{uid}", is_local=True, n_studies=1)
        s.add(d)
        s.flush()
        s.add(Study(study_uid=uid, accession=f"ACC{uid}", modality=modality, device_id=d.id,
                    study_datetime=NOW, source_kind="test", n_events=0))
        s.commit()
        return d.id


def test_device_writes_need_admin(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.patch("/api/devices/1", json={"origin": "external"}).status_code == 403
    assert client.post("/api/devices/merge", json={"record_ids": [1, 2], "survivor_id": 1}
                       ).status_code == 403


def test_merge_rename_split_dismiss_and_override(client):
    a = _add_device_with_study(client, "Ysio UPU", "25", "7.7.1")
    b = _add_device_with_study(client, "Ysio UPU", "25", "7.7.2")
    login(client, "boss", "boss-pass-123")
    sugg = client.get("/api/devices").json()["merge_suggestions"]
    assert any(set(x["record_ids"]) == {a, b} for x in sugg)

    r = client.post("/api/devices/merge", json={"record_ids": [a, b], "survivor_id": a,
                                                "display_name": "Ysio – Emergency"})
    assert r.status_code == 200 and r.json()["merged"] == [b]
    recs = {x["id"]: x for x in client.get("/api/devices").json()["records"]}
    assert recs[b]["physical_id"] == a
    rows = client.get("/api/studies", params={"q": "acc:ACC7.7.2"}).json()["rows"]
    assert rows[0]["device_name"] == "Ysio – Emergency" and rows[0]["device_id"] == a

    assert client.patch(f"/api/devices/{b}", json={"display_name": "Renamed"}).status_code == 200
    assert {x["id"]: x for x in client.get("/api/devices").json()["records"]}[a][
        "display_name"] == "Renamed"  # alias edits go to the physical device

    assert client.post(f"/api/devices/{b}/split").status_code == 200
    recs = {x["id"]: x for x in client.get("/api/devices").json()["records"]}
    assert recs[b]["physical_id"] == b
    assert client.post(f"/api/devices/{b}/split").status_code == 400  # not merged now

    client.post("/api/devices/merge-suggestions/dismiss", json={"record_ids": [b, a]})
    sugg = client.get("/api/devices").json()["merge_suggestions"]
    assert not any(set(x["record_ids"]) == {a, b} for x in sugg)

    assert client.get("/api/studies", params={"q": "acc:ACC7.7.2 modality:RF"}
                      ).json()["total"] == 0
    client.patch(f"/api/devices/{b}", json={"modality_override": "RF"})
    res = client.get("/api/studies", params={"q": "acc:ACC7.7.2 modality:RF"}).json()
    assert res["total"] == 1 and res["rows"][0]["modality"] == "RF"
    client.patch(f"/api/devices/{b}", json={"modality_override": "auto"})
    assert client.get("/api/studies", params={"q": "acc:ACC7.7.2 modality:DX"}
                      ).json()["total"] == 1

    assert client.patch(f"/api/devices/{b}", json={"origin": "elsewhere"}).status_code == 400
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        actions = {x.action for x in s.scalars(select(AuditLog))}
    assert {"device.merge", "device.update", "device.split",
            "device.merge_dismissed"} <= actions


def test_classification_review(client):
    login(client, "viewer", "viewer-pass-1")
    summary = {r["family"]: r for r in client.get("/api/classification/summary").json()}
    assert summary["rx"]["studies"] >= 1 and summary["rx"]["classified"] >= 1
    todo = client.get("/api/classification/unclassified?family=rx").json()
    spine = [x for x in todo if x["texts"]["protocol"] == "COLOANA"]
    assert spine and spine[0]["exposures"] == 1
    codes = {c["code"] for c in client.get("/api/codes/ro-nom-rx").json()}
    assert "RG-CVLM" in codes
    body = {"family": "rx", "field": "protocol", "text": "COLOANA", "code": "RG-CVLM"}
    assert client.put("/api/classification/mappings", json=body).status_code == 403

    login(client, "boss", "boss-pass-123")
    bad = client.put("/api/classification/mappings", json={**body, "code": "CT-CR"})
    assert bad.status_code == 400 and "not a code" in bad.json()["error"]
    r = client.put("/api/classification/mappings", json=body).json()
    assert r["studies_reclassified"] == 1
    left = client.get("/api/classification/unclassified?family=rx").json()
    assert not [x for x in left if x["texts"]["protocol"] == "COLOANA"]
    m = client.get("/api/classification/mappings").json()
    assert [(x["text"], x["code"]) for x in m] == [("coloana", "RG-CVLM")]


def test_stats_endpoint(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/stats", params={"q": "modality:DX", "metric": "dap",
                                         "by": "exam", "system": "ro-drl-ms838-2025"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "Gy.cm2" and body["level"] == "event"
    g = {x["key"]["exam"]: x for x in body["groups"]}
    assert g["RX-TORACE-PA-AP"]["median"] == 0.05
    bad = client.get("/api/stats", params={"metric": "nope"})
    assert bad.status_code == 400 and "unknown metric" in bad.json()["error"]


def test_drl_endpoints(client):
    login(client, "viewer", "viewer-pass-1")
    sets = {s["key"] for s in client.get("/api/drl/sets").json()}
    assert {"ro-ms838-2025", "ec-rp185-2018"} <= sets
    r = client.get("/api/drl/compare", params={"q": "", "min_n": 1}).json()
    assert r["set"] == "ro-ms838-2025" and r["by"] == "facility"
    assert client.get("/api/drl/compare", params={"set": "x"}).status_code == 400
    one = client.get("/api/drl/sets/ec-rp185-2018").json()
    assert one["key"] == "ec-rp185-2018" and len(one["values"]) > 0
    v = one["values"][0]
    assert {"exam", "exam_meaning", "band_label", "quantity", "value", "unit"} <= v.keys()
    assert client.get("/api/drl/sets/nope").status_code == 404


def test_export_roles_and_formats(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/export").status_code == 403  # needs exporter (or admin)
    login(client, "boss", "boss-pass-123")
    assert client.get("/api/export", params={"pid": "true"}).status_code == 403  # needs pid
    r = client.get("/api/export", params={"q": "modality:XA"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8")
    assert text.startswith("﻿Study ID,") and "P-ABCDE-12345" in text
    assert "POPESCU" not in text and len(text.strip().splitlines()) == 2
    x = client.get("/api/export", params={"q": "modality:XA", "format": "xlsx",
                                          "level": "event"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    with make_sessionmaker(make_engine(str(client.app.state.settings.database_url)))() as s:
        acts = [a.target for a in s.scalars(select(AuditLog).filter_by(action="export"))]
    assert "study:csv" in acts and "event:xlsx" in acts


def test_drl_dataset_and_summary_downloads(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/drl/dataset").status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/drl/dataset", params={"q": "", "format": "json"})
    assert r.status_code == 200 and r.json()["dataset"] == "dosimetrix-drl"
    s = client.get("/api/drl/summary", params={"q": "", "format": "csv", "min_n": 1})
    assert s.status_code == 200 and s.content.decode("utf-8-sig").startswith("unit,device,")
    assert client.get("/api/drl/summary", params={"set": "x"}).status_code == 400


def test_operators_report_is_admin_only(client):
    login(client, "phys", "phys-pass-12")
    assert client.get("/api/reports/operators").status_code == 403  # staff names
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/operators", params={"year": 2026, "quarter": 3, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["start"] == "2025-10-01" and r.json()["end"] == "2026-09-30"
    x = client.get("/api/reports/operators", params={"year": 2026, "quarter": 3,
                                                      "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/reports/operators", params={"year": 2026,
                                                        "quarter": 5}).status_code == 400


def test_insp1255_report_endpoint(client):
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/insp1255", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [s["key"] for s in body["sheets"]][:2] == ["radiologie", "ct"]
    x = client.get("/api/reports/insp1255", params={"year": 2026, "quarter": 4,
                                                     "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/reports/insp1255", params={"year": 2026,
                                                       "quarter": 7}).status_code == 400
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/reports/insp1255", params={"year": 2026,
                                                       "quarter": 1}).status_code == 403


def test_survey_export_endpoint(client):
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/survey/export", params={"month": "2026-11", "q": "", "format": "json"})
    assert r.status_code == 200 and r.json()["dataset"] == "dosimetrix-paediatric-survey"
    assert client.get("/api/survey/export", params={"month": "nov"}).status_code == 400


def test_quality_endpoint_and_study_findings(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/api/quality", params={"q": ""}).json()
    assert r["studies"] >= 1 and "ct_dlp_mismatch" in r["rules"]
    sid = client.get("/api/studies", params={"q": "modality:XA"}).json()["rows"][0]["id"]
    assert isinstance(client.get(f"/api/studies/{sid}").json()["quality"], list)


def test_alert_endpoints(client):
    from dosimetrix import alerts

    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        xa = s.scalar(select(Study).filter_by(study_uid="1.2.3"))
        xa.total_rp_dose = 6.0
        alerts.evaluate(s, [xa.id])
        s.commit()
    login(client, "viewer", "viewer-pass-1")
    open_ = client.get("/api/alerts").json()
    a = next(x for x in open_ if x["rule"] == "fluoro-kar-5gy")
    assert a["study_id"] and a["level"] == "alert"
    assert any(x["id"] == a["id"] for x in client.get("/api/overview").json()["alerts"])
    r = client.post(f"/api/alerts/{a['id']}/ack", json={"note": "seen, patient informed"})
    assert r.json()["status"] == "acknowledged"
    assert all(x["id"] != a["id"] for x in client.get("/api/alerts").json())
    rules = client.get("/api/alert-rules").json()
    rid = next(x["id"] for x in rules if x["key"] == "drl-2x")
    assert client.patch(f"/api/alert-rules/{rid}", json={"threshold": 3}).status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.patch(f"/api/alert-rules/{rid}", json={"threshold": 3, "severity": "alert"})
    assert r.json()["changes"]["threshold"] == {"from": 2.0, "to": 3.0}
    assert client.patch(f"/api/alert-rules/{rid}", json={"severity": "x"}).status_code == 400


def test_dicom_node_endpoints(client):
    login(client, "viewer", "viewer-pass-1")
    st = client.get("/api/dicom/status").json()
    assert st["local"]["aet"] and len(st["coverage"]) == 120
    assert client.post("/api/dicom/nodes", json={"name": "CT 1", "aet": "CT1"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    n = client.post("/api/dicom/nodes", json={"name": "CT 1", "aet": "CT1", "sends": True})
    assert n.status_code == 200, n.text
    nid = n.json()["id"]
    assert client.post("/api/dicom/nodes", json={"name": "X", "aet": "Y" * 20}).status_code == 400
    r = client.patch(f"/api/dicom/nodes/{nid}", json={"notes": "room 3"}).json()
    assert r["changes"]["notes"]["to"] == "room 3"
    assert "CT1" in client.get("/api/dicom/status").json()["local"]["allowed"]
    assert client.post(f"/api/dicom/nodes/{nid}/echo").status_code == 400  # no host
    assert client.delete(f"/api/dicom/nodes/{nid}").json() == {"deleted": "CT 1"}


def test_his_server_endpoints(client):
    from tests import his_stub

    stub = his_stub.Stub()
    try:
        login(client, "viewer", "viewer-pass-1")
        assert client.get("/api/his/status").json()["servers"] == []
        assert client.post("/api/his/servers", json={"name": "HIS"}).status_code == 403
        login(client, "boss", "boss-pass-123")
        assert client.post("/api/his/servers", json={"name": "HIS"}).status_code == 400
        r = client.post("/api/his/servers", json={
            "name": "HIS", "base_url": stub.url, "username": his_stub.USER, "password": "bad"})
        assert r.status_code == 200, r.text
        sid, body = r.json()["id"], r.json()
        assert body["password_set"] and "bad" not in r.text
        assert client.post(f"/api/his/servers/{sid}/check").json()["ok"] is False
        r = client.patch(f"/api/his/servers/{sid}", json={"password": his_stub.PASSWORD})
        assert r.json()["changes"] == {"password": "changed"}
        assert client.post(f"/api/his/servers/{sid}/check").json()["ok"] is True
        audit = client.get("/api/audit").text
        assert his_stub.PASSWORD not in audit and '"bad"' not in audit
        dev = client.get("/api/devices").json()["records"][0]
        assert dev["his_policy"] == "full"
        assert client.patch(f"/api/devices/{dev['id']}",
                            json={"his_policy": "patient"}).status_code == 200
        assert client.patch(f"/api/devices/{dev['id']}",
                            json={"his_policy": "maybe"}).status_code == 400
        sid_study = client.get("/api/studies").json()["rows"][0]["id"]
        r = client.post(f"/api/studies/{sid_study}/his")
        assert r.status_code == 200 and r.json()["status"] == "pending"
        assert client.get(f"/api/studies/{sid_study}").json()["his"]["status"] == "pending"
        assert client.delete(f"/api/his/servers/{sid}").json() == {"deleted": "HIS"}
    finally:
        stub.close()


def test_users_and_audit(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/users").status_code == 403
    assert client.get("/api/audit").status_code == 403
    login(client, "boss", "boss-pass-123")
    body = client.get("/api/users").json()
    assert "pid" in body["roles"] and any(u["username"] == "boss" for u in body["users"])
    bad = client.post("/api/users", json={"username": "nurse", "password": "short",
                                         "roles": ["viewer"]})
    assert bad.status_code == 400 and "10 characters" in bad.json()["error"]
    assert client.post("/api/users", json={"username": "nurse", "password": "long-enough-1",
                                           "roles": ["root"]}).status_code == 400
    r = client.post("/api/users", json={"username": "nurse", "name": "Nurse",
                                        "password": "long-enough-1", "roles": ["viewer"]})
    assert r.json()["roles"] == ["viewer"]
    r = client.patch("/api/users/nurse", json={"roles": ["viewer", "exporter"],
                                               "password": "another-pass-2"}).json()
    assert r["changes"]["password"] == "changed" and r["roles"] == ["exporter", "viewer"]
    assert client.patch("/api/users/boss", json={"active": False}).status_code == 400
    assert client.patch("/api/users/boss", json={"roles": ["viewer"]}).status_code == 400
    log = client.get("/api/audit", params={"action": "user."}).json()
    acts = [x["action"] for x in log["rows"]]
    assert acts[:2] == ["user.update", "user.create"]
    assert "another-pass" not in str(log) and "long-enough" not in str(log)
    client.cookies.clear()
    assert client.post("/api/login", json={"username": "nurse",
                                           "password": "another-pass-2"}).status_code == 200


def test_user_rename_and_remove(client):
    login(client, "boss", "boss-pass-123")
    assert client.post("/api/users", json={"username": "tech", "password": "tech-pass-123",
                                           "roles": ["viewer"]}).status_code == 200
    login(client, "tech", "tech-pass-123")
    client.get("/api/studies")
    tech_cookie = dict(client.cookies)
    login(client, "boss", "boss-pass-123")
    # rename: same rules as at creation; the renamed user stays signed in (cookie holds the id)
    assert client.patch("/api/users/tech", json={"username": "viewer"}).status_code == 400
    assert client.patch("/api/users/tech", json={"username": "a b"}).status_code == 400
    r = client.patch("/api/users/tech", json={"username": "radtech"}).json()
    assert r["username"] == "radtech"
    assert r["changes"]["username"] == {"from": "tech", "to": "radtech"}
    boss_cookie = dict(client.cookies)
    client.cookies.clear()
    client.cookies.update(tech_cookie)
    assert client.get("/api/me").json()["username"] == "radtech"
    assert client.delete("/api/users/viewer").status_code == 403  # not an admin
    client.cookies.clear()
    client.cookies.update(boss_cookie)
    assert client.post("/api/login", json={"username": "radtech",
                                           "password": "tech-pass-123"}).status_code == 200
    login(client, "boss", "boss-pass-123")
    # remove: never an admin (nor oneself); audit rows survive with the name they had
    assert client.delete("/api/users/boss").status_code == 400
    assert client.delete("/api/users/nobody").status_code == 400
    assert client.delete("/api/users/radtech").status_code == 200
    assert not any(u["username"] == "radtech" for u in client.get("/api/users").json()["users"])
    client.cookies.clear()
    client.cookies.update(tech_cookie)
    assert client.get("/api/studies").status_code == 401
    login(client, "boss", "boss-pass-123")
    log = client.get("/api/audit", params={"action": "user."}).json()["rows"]
    assert [x["action"] for x in log[:2]] == ["user.delete", "user.update"]
    assert log[0]["target"] == "user:radtech" and log[0]["detail"]["roles"] == "viewer"
    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        s.add(User(username="admin2", password_hash="x", roles="viewer,admin"))
        s.commit()
    r = client.delete("/api/users/admin2")
    assert r.status_code == 400 and "admin role" in r.json()["error"]


def test_cli_user_rename_and_delete(client):
    from dosimetrix import cli

    url = str(client.app.state.settings.database_url)
    base = ["user", "--url", url]
    with make_sessionmaker(make_engine(url))() as s:
        s.add(User(username="clerk", password_hash="x", roles="viewer"))
        s.commit()
    assert cli.main(base[:1] + ["rename", "clerk", "--to", "viewer"] + base[1:]) == 2
    assert cli.main(base[:1] + ["rename", "clerk", "--to", "physicist"] + base[1:]) == 0
    assert cli.main(base[:1] + ["delete", "boss"] + base[1:]) == 2
    assert cli.main(base[:1] + ["delete", "physicist"] + base[1:]) == 0
    assert cli.main(base[:1] + ["delete", "physicist"] + base[1:]) == 2
    with make_sessionmaker(make_engine(url))() as s:
        names = set(s.scalars(select(User.username)))
        assert "clerk" not in names and "physicist" not in names and "boss" in names
        acts = [a for (a,) in s.execute(select(AuditLog.action).where(
            AuditLog.username.like("cli:%")).order_by(AuditLog.id))]
        assert acts[-2:] == ["user.update", "user.delete"]


def test_alerts_page_api(client):
    from dosimetrix import alerts

    url = str(client.app.state.settings.database_url)
    with make_sessionmaker(make_engine(url))() as s:
        xa = s.scalar(select(Study).filter_by(study_uid="1.2.3"))
        xa.total_rp_dose, xa.total_dap = 7.0, 600.0
        alerts.evaluate(s, [xa.id])
        s.commit()
    login(client, "viewer", "viewer-pass-1")
    summ = client.get("/api/alerts/summary", params={"status": "all"}).json()
    assert summ["total"] >= 2 and summ["by_level"].get("alert", 0) >= 2
    keys = {r["rule"] for r in summ["by_rule"]}
    assert {"fluoro-kar-5gy", "fluoro-dap-500"} <= keys
    # selecting a rule must not shrink the rule dropdown
    sel = client.get("/api/alerts/summary",
                     params={"status": "all", "rule": "fluoro-dap-500"}).json()
    assert {r["rule"] for r in sel["by_rule"]} == keys and sel["total"] == 1
    one = client.get("/api/alerts", params={"status": "all", "rule": "fluoro-dap-500"}).json()
    assert len(one) == 1 and one[0]["modality"] == "XA"
    assert client.get("/api/alerts", params={"since": "bad"}).status_code == 400
    ids = [a["id"] for a in client.get("/api/alerts", params={"modality": "XA"}).json()]
    r = client.post("/api/alerts/ack", json={"ids": ids, "note": "reviewed"}).json()
    assert r["acknowledged"] == len(ids) > 0
    acked = client.get("/api/alerts", params={"status": "acknowledged", "modality": "XA"}).json()
    assert "reviewed" in {a["ack_note"] for a in acked}
    assert client.post("/api/alerts/ack", json={"ids": []}).status_code == 400


def test_grid_report_endpoint(client):
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/reports/grid", params={"year": 2026, "quarter": 4, "q": ""})
    assert r.status_code == 200, r.text
    assert r.json()["grid"].startswith("ICRP 135") and "rows" in r.json()
    x = client.get("/api/reports/grid", params={"year": 2026, "quarter": 4, "format": "xlsx"})
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert client.get("/api/reports/grid", params={"year": 2026, "quarter": 5}).status_code == 400
    s = client.get("/api/stats", params={"q": "", "metric": "dlp", "by": "exam,ref_group",
                                         "system": "ro-nom-ct"})
    assert s.status_code == 200 and "ref_group" in s.json()["orders"]
    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/reports/grid", params={"year": 2026, "quarter": 4}).status_code == 403


def test_sizes_import_csv(client):
    login(client, "viewer", "viewer-pass-1")
    csv = b"accession,weight_kg,source\nA1,86,measured\n"
    assert client.post("/api/import/sizes", content=csv,
                       headers={"content-type": "text/csv"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/import/sizes", content=csv, headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["counts"]["studies_updated"] == 1
    assert client.get("/api/studies/1").json()["patient"]["weight_kg"] == 88  # dry run
    r = client.post("/api/import/sizes?apply=1", content=csv,
                    headers={"content-type": "text/csv"})
    assert r.json()["applied"] and client.get("/api/studies/1").json()["patient"][
        "weight_kg"] == 86
    assert client.post("/api/import/sizes", content=b"x\n1\n",
                       headers={"content-type": "text/csv"}).status_code == 400


def test_study_rdsr_download(client):
    import io

    import pydicom

    login(client, "viewer", "viewer-pass-1")
    assert client.get("/api/studies/1/rdsr").status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.get("/api/studies/1/rdsr")
    assert r.status_code == 200 and r.headers["content-type"] == "application/dicom"
    ds = pydicom.dcmread(io.BytesIO(r.content))
    assert ds.PatientID == "P-ABCDE-12345" and ds.PatientName == ""
    assert client.get("/api/studies/1/rdsr?pid=1").status_code == 403


def test_indication_endpoints(client):
    login(client, "viewer", "viewer-pass-1")
    assert client.put("/api/studies/1/indication", json={"code": "RSP-PNM"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    assert client.put("/api/studies/1/indication", json={"code": "NOPE"}).status_code == 400
    r = client.put("/api/studies/1/indication", json={"code": "RSP-PNM"})
    assert r.status_code == 200 and r.json()["status"] == "manual"
    assert client.get("/api/studies/1").json()["indication"]["code"] == "RSP-PNM"
    assert client.put("/api/studies/1/indication", json={"code": None}).json() is None
    r = client.put("/api/indications/mappings", json={"text": "tuse seaca", "code": "PED-TUS"})
    assert r.status_code == 200 and r.json()["studies"] == 0
    rev = client.get("/api/indications/review").json()
    assert rev["decisions"][0]["code"] == "PED-TUS"
    client.put("/api/indications/mappings", json={"text": "tuse seaca", "code": None})


def test_survey_central_endpoints(client):
    from tests.test_aggregate import _csv

    login(client, "viewer", "viewer-pass-1")
    body = _csv("I-API", "2026-10", [{"dlp": 400}] * 10)
    assert client.post("/api/survey/uploads", content=body,
                       headers={"content-type": "text/csv"}).status_code == 403
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/survey/uploads?filename=a.csv", content=body,
                    headers={"content-type": "text/csv"})
    assert r.status_code == 200 and r.json()["rows"] == 10
    assert client.post("/api/survey/uploads", content=body,
                       headers={"content-type": "text/csv"}).status_code == 400  # same file
    assert client.put("/api/survey/institutions/I-API", json={"name": "Site A"}).status_code == 200
    rep = client.get("/api/survey/national", params={"start": "2026-01", "end": "2026-12"}).json()
    assert rep["rows"][0]["institutions"][0]["name"] == "Site A"
    up = client.get("/api/survey/uploads").json()[0]
    assert client.delete(f"/api/survey/uploads/{up['id']}").json()["rows"] == 10


def test_change_own_password(client):
    login(client, "phys", "phys-pass-12")
    put = lambda cur, new: client.put("/api/me/password", json={"current": cur, "new": new})  # noqa: E731
    assert put("nope", "x" * 12).status_code == 400
    assert put("phys-pass-12", "short").status_code == 400
    r = client.put("/api/me/password", json={"current": "phys-pass-12", "new": "phys-pass-new-1"})
    assert r.status_code == 200
    assert login(client, "phys", "phys-pass-new-1").status_code == 200
    client.put("/api/me/password", json={"current": "phys-pass-new-1", "new": "phys-pass-12"})
