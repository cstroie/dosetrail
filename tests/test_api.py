"""API behaviour on a small committed SQLite database."""

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from dosetrail.api.app import create_app
from dosetrail.config import Settings
from dosetrail.db.migrate import upgrade
from dosetrail.db.models import AuditLog, Device, Event, Patient, Study, User
from dosetrail.db.session import make_engine, make_sessionmaker
from dosetrail.security import hash_password

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


def test_studies_carry_pseudonyms_only(client):
    login(client, "viewer", "viewer-pass-1")
    body = client.get("/api/studies", params={"q": "modality:XA"}).text
    assert "P-ABCDE-12345" in body
    assert "POPESCU" not in body and "2041187" not in body and "1959" not in body
    res = client.get("/api/studies", params={"q": "pid:P-ABCDE-12345"}).json()
    assert res["total"] == 1 and res["rows"][0]["patient"]["age"] == 67


def test_detail_units(client):
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies").json()["rows"][0]["id"]
    d = client.get(f"/api/studies/{sid}").json()
    ev = d["events"][0]
    assert ev["kar_mgy"] == 123.0 and ev["field_cm"] == 20.0 and ev["sid_cm"] == 105.0
    assert d["thresholds"]["source"] == "default"
    assert "identified" not in d["patient"] and "POPESCU" not in str(d)


def test_identify_needs_pid_role_and_is_audited(client):
    login(client, "viewer", "viewer-pass-1")
    sid = client.get("/api/studies").json()["rows"][0]["id"]
    assert client.post(f"/api/studies/{sid}/identify").status_code == 403
    login(client, "phys", "phys-pass-12")
    r = client.post(f"/api/studies/{sid}/identify").json()
    assert r["name"] == "POPESCU^ION" and r["id"] == "2041187"


def test_audit_entry_written(client, tmp_path_factory):
    login(client, "phys", "phys-pass-12")
    sid = client.get("/api/studies").json()["rows"][0]["id"]
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
