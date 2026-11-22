"""First-run setup wizard: token, empty database, first admin, site, finish."""

from fastapi.testclient import TestClient

from dosetrail.api.app import create_app
from dosetrail.config import Settings
from dosetrail.db.session import make_engine

SECRET = b"s" * 32


def _app(tmp_path):
    url = f"sqlite:///{tmp_path / 'fresh.sqlite3'}"
    s = Settings(database_url=url, data_dir=tmp_path / "data",
                 secret_file=tmp_path / "data" / "secret")
    app = create_app(s, engine=make_engine(url), secret=SECRET)
    return app, TestClient(app)


def test_first_run(tmp_path, monkeypatch):
    app, c = _app(tmp_path)
    token = app.state.setup_token
    assert token and (tmp_path / "data" / "setup-token").read_text().strip() == token
    # every page leads to the wizard; it is open while no account exists
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert c.get("/login", follow_redirects=False).headers["location"] == "/setup"
    assert c.get("/setup").status_code == 200
    st = c.get("/api/setup/status").json()
    assert st["schema"] is False and st["needs_admin"]
    # the token protects every write
    assert c.post("/api/setup/database", json={"token": "nope"}).status_code == 400
    assert c.post("/api/setup/database", json={"token": token}).status_code == 200
    st = c.get("/api/setup/status").json()
    assert {x["key"]: x["ok"] for x in st["checks"]}["seed"]
    bad = {"token": "nope", "username": "root", "password": "long-enough-1"}
    assert c.post("/api/setup/admin", json=bad).status_code == 400
    short = {"token": token, "username": "root", "password": "short"}
    assert c.post("/api/setup/admin", json=short).status_code == 400
    r = c.post("/api/setup/admin", json={**short, "password": "long-enough-1"})
    assert r.status_code == 200 and "admin" in r.json()["roles"]
    assert not (tmp_path / "data" / "setup-token").exists() or app.state.setup_token is None
    # signed in now: the later steps are admin calls
    assert c.post("/api/setup/site", json={"name": "Test Hospital", "code": "th1"}).json() \
        == {"name": "Test Hospital", "code": "TH1"}
    from dosetrail import setup

    monkeypatch.setattr(setup, "install_growth", lambda settings: ["ok"])
    assert c.post("/api/setup/growth").json() == {"report": ["ok"]}
    assert c.post("/api/setup/finish").json() == {"done": True}
    assert c.get("/api/setup/status").json()["done"]
    # a second admin cannot be created through the wizard, and strangers can't see it
    assert c.post("/api/setup/admin", json={**short, "password": "long-enough-2",
                                            "username": "x"}).status_code == 400
    c.cookies.clear()
    assert c.get("/api/setup/status").status_code == 401
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/login").status_code == 200
