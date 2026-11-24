"""Web hardening: headers, login throttle, session revocation, same-origin writes, size caps."""

import pytest
from fastapi.testclient import TestClient

from dozimetrix.api.app import create_app
from dozimetrix.api.hardening import LoginThrottle
from dozimetrix.config import Settings
from dozimetrix.db.migrate import upgrade
from dozimetrix.db.models import User
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.security import hash_password


@pytest.fixture()
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'h.sqlite3'}"
    upgrade(url)
    engine = make_engine(url)
    with make_sessionmaker(engine)() as s:
        s.add_all([
            User(username="viewer", password_hash=hash_password("viewer-pass-1"), roles="viewer"),
            User(username="boss", password_hash=hash_password("boss-pass-123"),
                 roles="viewer,admin"),
        ])
        s.commit()
    settings = Settings(database_url=url, data_dir=tmp_path, specs_dir=tmp_path / "specs",
                        plugins_dir=tmp_path / "plugins", max_upload_mb=0.01)
    return TestClient(create_app(settings, engine=engine, secret=b"k" * 32))


def login(client, user, pw):
    client.cookies.clear()
    r = client.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def test_security_headers_on_pages_and_api(client):
    login(client, "viewer", "viewer-pass-1")
    for path in ("/", "/api/me", "/login"):
        r = client.get(path)
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"
    assert "<script type=\"module\">" not in client.get("/login").text  # no inline scripts


def test_host_header_is_escaped_in_pages(client):
    login(client, "viewer", "viewer-pass-1")
    r = client.get("/", headers={"host": 'evil"><script>x</script>'})
    assert "<script>x</script>" not in r.text


def test_login_is_throttled_per_username():
    t = LoginThrottle(max_failures=3, max_per_ip=100)

    class Req:
        client = type("C", (), {"host": "1.2.3.4"})

    for _ in range(3):
        t.check(Req, "login", "boss")
        t.fail(Req, "login", "boss")
    with pytest.raises(Exception) as e:
        t.check(Req, "login", "boss")
    assert e.value.status_code == 429 and int(e.value.headers["Retry-After"]) > 0
    t.check(Req, "login", "viewer")  # another username is not locked


def test_login_throttle_through_the_api(client):
    for _ in range(5):
        r = client.post("/api/login", json={"username": "boss", "password": "wrong-password"})
        assert r.status_code == 401
    r = client.post("/api/login", json={"username": "boss", "password": "boss-pass-123"})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_password_change_ends_other_sessions(client):
    login(client, "viewer", "viewer-pass-1")
    old = client.cookies.get("dt_session")
    r = client.put("/api/me/password", json={"current": "viewer-pass-1", "new": "viewer-pass-2"})
    assert r.status_code == 200
    assert client.get("/api/me").status_code == 200  # this browser got a fresh cookie
    client.cookies.clear()
    client.cookies.set("dt_session", old)
    assert client.get("/api/me").status_code == 401  # the old token no longer works


def test_role_change_ends_sessions(client):
    login(client, "viewer", "viewer-pass-1")
    viewer_cookie = client.cookies.get("dt_session")
    login(client, "boss", "boss-pass-123")
    r = client.patch("/api/users/viewer", json={"roles": ["viewer", "exporter"]})
    assert r.status_code == 200
    client.cookies.clear()
    client.cookies.set("dt_session", viewer_cookie)
    assert client.get("/api/me").status_code == 401


def test_cross_origin_writes_are_refused(client):
    login(client, "boss", "boss-pass-123")
    body = {"year": 2026, "quarter": 3, "finding": "f", "action": "a"}
    r = client.post("/api/actions", json=body, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/actions", json=body, headers={"origin": "http://testserver"})
    assert r.status_code == 200


def test_viewer_cannot_write_actions_or_list_uploads(client):
    login(client, "viewer", "viewer-pass-1")
    body = {"year": 2026, "quarter": 3, "finding": "f", "action": "a"}
    assert client.post("/api/actions", json=body).status_code == 403
    assert client.post("/api/alerts/ack", json={"ids": [1]}).status_code == 403
    assert client.get("/api/survey/uploads").status_code == 403


def test_oversized_body_is_refused(client):
    login(client, "boss", "boss-pass-123")
    r = client.post("/api/import/sizes", content=b"x" * 20000,
                    headers={"content-type": "text/csv"})
    assert r.status_code == 413


def test_live_rejects_too_many_topics(client):
    login(client, "viewer", "viewer-pass-1")
    topics = ",".join(f"study:{i}" for i in range(30))
    assert client.get("/api/live", params={"topics": topics}).status_code == 400


def test_writes_through_a_proxy_that_rewrites_host(client):
    login(client, "boss", "boss-pass-123")
    body = {"year": 2026, "quarter": 3, "finding": "f", "action": "a"}
    r = client.post("/api/actions", json=body,
                    headers={"origin": "https://dose.example", "x-forwarded-host": "dose.example"})
    assert r.status_code == 200
