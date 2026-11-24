"""FastAPI application: serves the static UI and the JSON API.

Privacy rules:
* every /api route except /api/login requires a session;
* responses carry patient pseudonyms only; identifiers need the `pid` role and an explicit,
  audit-logged call (POST /api/studies/{id}/identify);
* the server is meant to bind to localhost or sit behind an HTTPS reverse proxy, and the
  access log is disabled by `dozimetrix serve` because query strings may contain identifiers.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import Settings, load_settings
from ..db.models import AuditLog, DicomNode, Study, User
from ..db.session import make_engine, make_sessionmaker
from ..security import load_or_create_secret, read_session, sign_session, verify_password
from . import admin, data, review

WEB = Path(__file__).resolve().parent.parent / "web"
LOGIN_HTML = Path(__file__).resolve().parent / "login.html"
SETUP_HTML = Path(__file__).resolve().parent / "setup.html"
COOKIE = "dt_session"
PAGES = {"": "index.html", "index.html": "index.html", "studies.html": "studies.html",
         "study.html": "study.html", "charts.html": "charts.html",
         "devices.html": "devices.html", "drl.html": "drl.html", "drl-sets.html": "drl-sets.html",
         "review.html": "review.html",
         "reports.html": "reports.html", "quality.html": "quality.html",
         "rules.html": "rules.html", "integration.html": "integration.html",
         "users.html": "users.html", "audit.html": "audit.html",
         "alerts.html": "alerts.html", "local-drl.html": "local-drl.html",
         "indications.html": "indications.html", "survey-central.html": "survey-central.html",
         "dose-statement.html": "dose-statement.html"}
OLD_PAGES = {"dicom.html": "integration.html"}
LIVE_TOPICS = {"pulse", "services", "dicom", "his", "studies", "alerts"}
LIVE_TOPIC_EVERY_S = {"studies": 15, "alerts": 60}  # list pages: scarce updates are enough
LIVE_EVERY_S = 5  # heartbeats come every minute or so, HIS lookups every few seconds
LIVE_PING_S = 25
LIVE_RETRY_MS = 5000
LIVE_MAX_S = 600  # a stream ends after 10 min and the browser reopens it: no stale sockets  # old name -> page (DICOM, HIS and more now)


def _host_ips() -> list[str]:
    """This machine's IPv4 addresses (what modalities and PACS must be told to send to)."""
    import socket

    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address of the default route, no packet is sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("10.255.255.255", 1))
            ips.add(sk.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


class RevalidatedStatic(StaticFiles):
    """Static files that the browser must revalidate (ETag / 304) on every load: without a
    Cache-Control header it caches scripts heuristically and keeps showing an old page after
    an update."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class LoginIn(BaseModel):
    username: str
    password: str


class LocalDRLIn(BaseModel):
    name: str
    grid: str = "national"
    start: dt.date
    end: dt.date
    min_n: int = 10
    q: str = "origin:local"
    estimated: bool = False
    rows: list[dict]


class ActionIn(BaseModel):
    year: int | None = None
    quarter: int | None = None
    finding: str | None = None
    action: str | None = None
    owner: str | None = None
    status: str | None = None  # open | doing | done


class GlandularityIn(BaseModel):
    percent: float | None = None  # 0..100, None removes it


class IndicationIn(BaseModel):
    code: str | None = None


class IndicationMapIn(BaseModel):
    text: str
    code: str | None = None


class InstitutionNameIn(BaseModel):
    name: str | None = None


class PasswordIn(BaseModel):
    current: str
    new: str


class TokenIn(BaseModel):
    token: str


class SetupAdminIn(BaseModel):
    token: str
    username: str
    password: str
    name: str | None = None


class SiteIn(BaseModel):
    name: str
    code: str | None = None


class DevicePatch(BaseModel):
    display_name: str | None = None
    modality_override: str | None = None
    origin: str | None = None
    his_policy: str | None = None


class MergeIn(BaseModel):
    record_ids: list[int]
    survivor_id: int
    display_name: str | None = None


class DismissIn(BaseModel):
    record_ids: list[int]


class UserIn(BaseModel):
    username: str | None = None
    name: str | None = None
    roles: list[str] | None = None
    active: bool | None = None
    password: str | None = None


class NodeIn(BaseModel):
    name: str | None = None
    aet: str | None = None
    host: str | None = None
    port: int | None = None
    enabled: bool | None = None
    query_retrieve: bool | None = None
    sends: bool | None = None
    retrieve: str | None = None
    move_destination: str | None = None
    modalities: str | None = None
    max_results: int | None = None
    timeout: int | None = None
    sync_days: int | None = None
    notes: str | None = None


class HisServerIn(BaseModel):
    name: str | None = None
    kind: str | None = None
    base_url: str | None = None
    username: str | None = None
    password: str | None = None  # write-only; "" clears it
    enabled: bool | None = None
    timeout: int | None = None
    modalities: str | None = None
    window_before_h: int | None = None
    window_after_h: int | None = None
    min_interval_s: float | None = None
    backfill_hours: str | None = None
    notes: str | None = None


class AckIn(BaseModel):
    note: str | None = None


class AckManyIn(BaseModel):
    ids: list[int]
    note: str | None = None


class RulePatch(BaseModel):
    enabled: bool | None = None
    threshold: float | None = None
    severity: str | None = None
    notify: bool | None = None
    window_days: int | None = None
    max_age_days: int | None = None
    name: str | None = None


class DecisionIn(BaseModel):
    family: str  # rx | rf | ct
    field: str  # protocol | anatomy | study_description | procedure_name | ...
    text: str
    code: str | None = None  # nomenclator code; null clears the decision


def create_app(settings: Settings | None = None, engine: Engine | None = None,
               secret: bytes | None = None, now: dt.datetime | None = None) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or make_engine(settings.database_url)
    maker = make_sessionmaker(engine)
    secret = secret or load_or_create_secret(settings.secret_file)
    app = FastAPI(title="Dozimetrix", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.secret = secret
    app.state.settings = settings
    app.state.fixed_now = now  # tests only
    from ..specs import plugins as _plugins

    app.state.plugins = _plugins.load(settings.plugins_dir)  # trusted code, see specs/plugins.py
    if app.state.plugins:
        with maker() as s0:
            for pl in app.state.plugins:
                s0.add(AuditLog(username="system", action="plugin", target=pl["file"],
                                detail={"sha256": pl["sha256"], "error": pl["error"]}))
            s0.commit()

    def db() -> Iterator[Session]:
        with maker() as session:
            yield session

    def current_user(request: Request, session: Session = Depends(db)) -> User:
        uid = read_session(secret, request.cookies.get(COOKIE))
        user = session.get(User, uid) if uid else None
        if user is None or not user.is_active:
            raise HTTPException(401, "login required")
        return user

    def admin_user(user: User = Depends(current_user)) -> User:
        if "admin" not in user.role_set():
            raise HTTPException(403, "the admin role is required")
        return user

    def admin_write(session: Session, request: Request, user: User, action: str,
                    target: str, fn):
        try:
            result = fn()
        except admin.AdminError as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, action, target, result)
        session.commit()
        return result

    def clock() -> dt.datetime:
        return app.state.fixed_now or dt.datetime.now()

    # ------------------------------------------------------------------ first-run setup

    from .. import setup as first_run

    app.state.setup_token = None

    def setup_token(session: Session) -> str | None:
        """The one-time token while no account exists (issued at start, or lazily)."""
        if not first_run.needs_admin(session):
            return None
        if app.state.setup_token is None:
            app.state.setup_token = first_run.issue_token(settings.data_dir)
        return app.state.setup_token

    with maker() as _s:
        setup_token(_s)

    def optional_user(request: Request, session: Session) -> User | None:
        uid = read_session(secret, request.cookies.get(COOKIE))
        user = session.get(User, uid) if uid else None
        return user if user is not None and user.is_active else None

    def setup_guard(request: Request, session: Session) -> User | None:
        """No account yet: open (the token protects the first write). Else: admin only."""
        if first_run.needs_admin(session):
            return None
        user = optional_user(request, session)
        if user is None:
            raise HTTPException(401, "login required")
        if "admin" not in user.role_set():
            raise HTTPException(403, "the admin role is required")
        return user

    def _setup(fn):
        try:
            return fn()
        except first_run.SetupError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/setup/status")
    def setup_status(request: Request, session: Session = Depends(db)):
        setup_guard(request, session)
        return first_run.status(session, settings)

    @app.post("/api/setup/database")
    def setup_database(body: TokenIn, session: Session = Depends(db)):
        """Fresh install only (no account yet; token): create the schema, load seeds."""
        _setup(lambda: first_run.check_token(setup_token(session), body.token))
        report = first_run.prepare_database(session, settings)
        session.commit()
        return {"report": report}

    @app.post("/api/setup/admin")
    def setup_admin(body: SetupAdminIn, request: Request, response: Response,
                    session: Session = Depends(db)):
        _setup(lambda: first_run.check_token(setup_token(session), body.token))
        user = _setup(lambda: first_run.create_admin(session, body.model_dump()))
        _audit(session, request, user, "setup.admin", user.username)
        session.commit()
        app.state.setup_token = None
        response.set_cookie(COOKIE, sign_session(secret, user.id, settings.session_hours),
                            httponly=True, samesite="strict",
                            secure=settings.secure_cookies or request.url.scheme == "https",
                            max_age=int(settings.session_hours * 3600), path="/")
        return _me(user)

    @app.post("/api/setup/site")
    def setup_site(body: SiteIn, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        out = _setup(lambda: first_run.save_site(session, body.model_dump()))
        _audit(session, request, user, "setup.site", out["code"], out)
        session.commit()
        return out

    @app.post("/api/setup/growth")
    def setup_growth(request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        report = _setup(lambda: first_run.install_growth(settings))
        _audit(session, request, user, "setup.growth", "install", {"report": report})
        session.commit()
        return {"report": report}

    @app.post("/api/setup/finish")
    def setup_finish(request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        first_run.finish(session, settings)
        _audit(session, request, user, "setup.finish", None)
        session.commit()
        return {"done": True}

    # ------------------------------------------------------------------ auth

    @app.post("/api/login")
    def login(body: LoginIn, request: Request, response: Response,
              session: Session = Depends(db)):
        user = session.scalar(select(User).filter_by(username=body.username))
        ok = bool(user and user.is_active and verify_password(body.password, user.password_hash))
        _audit(session, request, user if ok else None, "login" if ok else "login_failed",
               body.username)
        session.commit()
        if not ok:
            raise HTTPException(401, "invalid username or password")
        user.last_login_at = dt.datetime.now()
        session.commit()
        response.set_cookie(COOKIE, sign_session(secret, user.id, settings.session_hours),
                            httponly=True, samesite="strict",
                            # always Secure over HTTPS (X-Forwarded-Proto from a trusted proxy)
                            secure=settings.secure_cookies or request.url.scheme == "https",
                            max_age=int(settings.session_hours * 3600), path="/")
        return _me(user)

    @app.post("/api/logout")
    def logout(response: Response):
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return _me(user)

    # a user's own settings on the server (chart workspaces): only "workspace*" keys, small
    PREF_KEY = re.compile(r"^workspace[A-Za-z0-9._ \-]{0,100}$")

    @app.get("/api/me/prefs/{key}")
    def get_pref(key: str, user: User = Depends(current_user),
                 session: Session = Depends(db)):
        from ..db.models import UserPref

        if not PREF_KEY.match(key):
            raise HTTPException(400, "unknown preference")
        row = session.scalar(select(UserPref).filter_by(user_id=user.id, key=key))
        return {"key": key, "value": row.value if row else None}

    @app.put("/api/me/prefs/{key}")
    def put_pref(key: str, body: dict = Body(...), user: User = Depends(current_user),  # noqa: B008
                 session: Session = Depends(db)):
        from ..db.models import UserPref

        if not PREF_KEY.match(key):
            raise HTTPException(400, "unknown preference")
        value = body.get("value")
        if len(json.dumps(value)) > 256_000:
            raise HTTPException(413, "preference too large")
        row = session.scalar(select(UserPref).filter_by(user_id=user.id, key=key))
        if row is None:
            row = UserPref(user_id=user.id, key=key)
            session.add(row)
        row.value = value
        row.updated_at = clock()
        session.commit()
        return {"key": key, "saved": True}

    # ------------------------------------------------------------------ data

    @app.get("/api/overview")
    def overview(user: User = Depends(current_user), session: Session = Depends(db)):
        return data.overview(session, clock(), settings.specs_dir)

    @app.get("/api/devices")
    def devices(user: User = Depends(current_user), session: Session = Depends(db)):
        return data.devices(session)

    @app.patch("/api/devices/{device_id}")
    def device_patch(device_id: int, body: DevicePatch, request: Request,
                     user: User = Depends(admin_user), session: Session = Depends(db)):
        patch = body.model_dump(exclude_unset=True)
        return admin_write(session, request, user, "device.update", f"device:{device_id}",
                           lambda: admin.update_device(session, device_id, patch))

    @app.post("/api/devices/merge")
    def device_merge(body: MergeIn, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        return admin_write(session, request, user, "device.merge",
                           f"device:{body.survivor_id}",
                           lambda: admin.merge_devices(session, body.record_ids,
                                                       body.survivor_id, body.display_name))

    @app.post("/api/devices/{device_id}/split")
    def device_split(device_id: int, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        return admin_write(session, request, user, "device.split", f"device:{device_id}",
                           lambda: admin.split_device(session, device_id))

    @app.post("/api/devices/merge-suggestions/dismiss")
    def device_dismiss(body: DismissIn, request: Request, user: User = Depends(admin_user),
                       session: Session = Depends(db)):
        return admin_write(session, request, user, "device.merge_dismissed",
                           ",".join(map(str, body.record_ids)),
                           lambda: admin.dismiss_suggestion(session, body.record_ids))

    @app.get("/api/drl")
    def drl(user: User = Depends(current_user)):
        return data.drl()

    @app.get("/api/drl/sets")
    def drl_sets(user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        return drl_mod.reference_sets(session)

    @app.get("/api/drl/sets/{key}")
    def drl_set(key: str, user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        try:
            return drl_mod.reference_values(session, key)
        except drl_mod.DRLError as e:
            raise HTTPException(404, str(e)) from e

    def _period(start: dt.date | None, end: dt.date | None) -> tuple[dt.date, dt.date]:
        end = end or clock().date()
        start = start or end - dt.timedelta(days=365)
        if start > end:
            raise HTTPException(400, "start is after end")
        return start, end

    def _period_conds(q: str, start: dt.date, end: dt.date, session: Session) -> list:
        return [*data.checked_conditions(q, clock(), data.overrides(session)),
                Study.study_datetime >= dt.datetime.combine(start, dt.time()),
                Study.study_datetime < dt.datetime.combine(end + dt.timedelta(days=1),
                                                           dt.time())]

    @app.get("/api/drl/local/propose")
    def local_drl_propose(q: str = "origin:local", start: dt.date | None = None,
                          end: dt.date | None = None, grid: str = "national",
                          min_n: int = 10, estimated: bool = False,
                          user: User = Depends(current_user), session: Session = Depends(db)):
        """Proposed local DRLs: P75 of the room medians (ICRP 135); see localdrl.py."""
        from .. import localdrl

        start, end = _period(start, end)
        try:
            out = localdrl.propose(session, _period_conds(q, start, end, session), grid,
                                   max(min_n, 1), estimated)
        except localdrl.LocalDRLError as e:
            raise HTTPException(400, str(e)) from e
        return {**out, "start": start.isoformat(), "end": end.isoformat(), "q": q}

    @app.post("/api/drl/local")
    def local_drl_adopt(body: LocalDRLIn, request: Request, user: User = Depends(admin_user),
                        session: Session = Depends(db)):
        from .. import localdrl

        try:
            rs = localdrl.adopt(session, body.rows, name=body.name, grid=body.grid,
                                start=body.start, end=body.end, min_n=body.min_n,
                                query=body.q + ("" if body.estimated
                                                else " (estimated weights left out)"),
                                now=clock())
        except localdrl.LocalDRLError as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "drl.local.adopt", rs.key,
               {"name": rs.name, "values": len(rs.values), "grid": body.grid,
                "start": body.start.isoformat(), "end": body.end.isoformat()})
        session.commit()
        return {"key": rs.key, "name": rs.name, "values": len(rs.values)}

    @app.delete("/api/drl/sets/{key}")
    def drl_set_delete(key: str, request: Request, user: User = Depends(admin_user),
                       session: Session = Depends(db)):
        from .. import localdrl

        try:
            out = localdrl.delete(session, key)
        except localdrl.LocalDRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "drl.local.delete", key, out)
        session.commit()
        return out

    @app.get("/api/drl/local/review")
    def local_drl_review(request: Request, set: str, q: str = "origin:local",
                         start: dt.date | None = None, end: dt.date | None = None,
                         min_n: int = 10, format: str = "json",
                         user: User = Depends(current_user), session: Session = Depends(db)):
        """An adopted local set against a new period (yearly review); json or xlsx."""
        from .. import localdrl

        start, end = _period(start, end)
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        try:
            rep = localdrl.review(session, set, _period_conds(q, start, end, session),
                                  max(min_n, 1))
        except localdrl.LocalDRLError as e:
            raise HTTPException(404, str(e)) from e
        rep = {**rep, "start": start.isoformat(), "end": end.isoformat(), "q": q}
        if format == "json":
            return rep
        _exporter(user)
        _audit(session, request, user, "report", f"drl.local.review:{set}", {"q": q})
        session.commit()
        info = {"Perioada": f"{start.isoformat()} – {end.isoformat()}", "Selecție": q,
                "Generat": clock().isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username}
        return _file(lambda fh: localdrl.write_review_xlsx(rep, fh, info), "xlsx",
                     f"revizuire-nrd-local-{end:%Y%m%d}", clock())

    @app.get("/api/drl/compare")
    def drl_compare(q: str = "origin:local", set: str = "ro-ms838-2025", by: str = "facility",
                    min_n: int = 20, estimated: bool = False,
                    user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        try:
            return data.drl_compare(session, q, clock(), set, by, max(min_n, 1), estimated)
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/modalities")
    def modalities(user: User = Depends(current_user)):
        from ..modality_profiles import PROFILES

        return PROFILES

    @app.get("/api/studies")
    def studies(q: str = "", sort: str = "date", dir: str = "desc", page: int = 1,
                size: int = 50, user: User = Depends(current_user),
                session: Session = Depends(db)):
        return data.studies(session, q, sort, dir, max(page, 1), min(max(size, 1), 500), clock())

    @app.get("/api/studies/{study_id}")
    def study(study_id: int, user: User = Depends(current_user),
              session: Session = Depends(db)):
        from .. import quality

        out = data.study(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
        out["quality"] = quality.study_findings(session, study_id)
        from .. import indications

        out["indication"] = indications.of_study(session, study_id)
        return out

    # ------------------------------------------------------------------ plan of measures

    ACTION_STATUS = ("open", "doing", "done")

    def _action_dict(a) -> dict:
        return {"id": a.id, "year": a.year, "quarter": a.quarter, "finding": a.finding,
                "action": a.action, "owner": a.owner, "status": a.status,
                "created_by": a.created_by,
                "updated_at": a.updated_at.isoformat(timespec="minutes") if a.updated_at else None}

    @app.get("/api/actions")
    def actions_list(year: int, quarter: int, user: User = Depends(current_user),
                     session: Session = Depends(db)):
        """The quarter's plan of optimisation measures (art. 7(2))."""
        from ..db.models import OptimizationAction as A

        rows = session.scalars(select(A).where(A.year == year, A.quarter == quarter)
                               .order_by(A.id))
        return {"year": year, "quarter": quarter, "rows": [_action_dict(a) for a in rows]}

    @app.post("/api/actions")
    def actions_add(body: ActionIn, request: Request, user: User = Depends(current_user),
                    session: Session = Depends(db)):
        from ..db.models import OptimizationAction as A

        _exporter(user)
        text_ok = (body.finding or "").strip() and (body.action or "").strip()
        if body.year is None or body.quarter not in (1, 2, 3, 4) or not text_ok:
            raise HTTPException(400, "year, quarter 1..4, finding and action are required")
        if (body.status or "open") not in ACTION_STATUS:
            raise HTTPException(400, "status is open|doing|done")
        a = A(year=body.year, quarter=body.quarter, finding=body.finding.strip()[:1000],
              action=body.action.strip()[:1000], owner=(body.owner or "").strip()[:120] or None,
              status=body.status or "open", created_by=user.username)
        session.add(a)
        session.flush()
        _audit(session, request, user, "action.add", f"action:{a.id}",
               {"period": f"{a.year}Q{a.quarter}"})
        session.commit()
        return _action_dict(a)

    @app.put("/api/actions/{action_id}")
    def actions_edit(action_id: int, body: ActionIn, request: Request,
                     user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import OptimizationAction as A

        _exporter(user)
        a = session.get(A, action_id)
        if a is None:
            raise HTTPException(404, "action not found")
        if body.status is not None and body.status not in ACTION_STATUS:
            raise HTTPException(400, "status is open|doing|done")
        changed = {}
        for k in ("finding", "action", "owner", "status"):
            v = getattr(body, k)
            if v is not None and v.strip() != (getattr(a, k) or ""):
                changed[k] = v.strip()[:1000]
                setattr(a, k, v.strip()[:1000] or None)
        if not a.finding or not a.action:
            raise HTTPException(400, "finding and action cannot be empty")
        _audit(session, request, user, "action.edit", f"action:{a.id}", changed)
        session.commit()
        return _action_dict(a)

    @app.delete("/api/actions/{action_id}")
    def actions_delete(action_id: int, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        from ..db.models import OptimizationAction as A

        _exporter(user)
        a = session.get(A, action_id)
        if a is None:
            raise HTTPException(404, "action not found")
        _audit(session, request, user, "action.delete", f"action:{a.id}",
               {"period": f"{a.year}Q{a.quarter}", "action": a.action[:120]})
        session.delete(a)
        session.commit()
        return {"deleted": action_id}

    @app.post("/api/deadlines/{deadline_id}/done")
    def deadline_done(deadline_id: str, request: Request, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        """Mark a reporting deadline as sent: only a person knows that (nothing is sent)."""
        from .. import deadlines
        from ..db.models import AppSetting

        _exporter(user)
        from ..specs import loader as _loader

        known = {d["id"] for d in deadlines.upcoming(
            clock().date(), (), list(_loader.discover(settings.specs_dir).specs.values()))}
        if deadline_id not in known:
            raise HTTPException(404, "no such deadline in its window")
        row = session.get(AppSetting, deadlines.DONE_KEY) or AppSetting(key=deadlines.DONE_KEY)
        row.value = sorted({*(row.value or []), deadline_id})
        session.add(row)
        _audit(session, request, user, "deadline.done", deadline_id)
        session.commit()
        return {"done": deadline_id}

    @app.put("/api/studies/{study_id}/glandularity")
    def study_glandularity(study_id: int, body: GlandularityIn, request: Request,
                           user: User = Depends(admin_user), session: Session = Depends(db)):
        """The breast glandularity of a mammography study, typed in (DICOM does not carry it)."""
        st = session.get(Study, study_id)
        if st is None:
            raise HTTPException(404, "study not found")
        if st.modality != "MG":
            raise HTTPException(400, "glandularity is for mammography studies")
        if body.percent is not None and not 0 <= body.percent <= 100:
            raise HTTPException(400, "percent is 0..100")
        before = st.glandularity_pct
        st.glandularity_pct = body.percent
        _audit(session, request, user, "glandularity", f"study:{study_id}",
               {"from": before, "to": body.percent})
        session.commit()
        return {"study_id": study_id, "glandularity_pct": st.glandularity_pct}

    @app.put("/api/studies/{study_id}/indication")
    def study_indication(study_id: int, body: IndicationIn, request: Request,
                         user: User = Depends(admin_user), session: Session = Depends(db)):
        """Code one study by hand (``code`` null: back to automatic matching)."""
        from .. import indications

        if session.get(Study, study_id) is None:
            raise HTTPException(404, "study not found")
        code = _indication_code(session, body.code)
        indications.set_study(session, study_id, code)
        _audit(session, request, user, "indication.study", f"study:{study_id}",
               {"code": body.code})
        session.commit()
        return indications.of_study(session, study_id)

    def _indication_code(session: Session, code: str | None):
        from ..db.models import Code, CodeSystem
        from ..indications import SYSTEM

        if code is None:
            return None
        c = session.scalar(select(Code).join(CodeSystem).where(
            CodeSystem.key == SYSTEM, Code.code == code))
        if c is None:
            raise HTTPException(400, f"unknown indication code {code!r}")
        return c

    @app.get("/api/indications/review")
    def indications_review(limit: int = 200, user: User = Depends(admin_user),
                           session: Session = Depends(db)):
        from .. import indications

        return indications.review(session, min(max(limit, 1), 1000))

    @app.put("/api/indications/mappings")
    def indications_decide(body: IndicationMapIn, request: Request,
                           user: User = Depends(admin_user), session: Session = Depends(db)):
        """One text -> one code for every study with it (``code`` null removes it)."""
        from .. import indications

        code = _indication_code(session, body.code)
        indications.set_mapping(session, body.text, code)
        ids = indications.studies_with_text(session, body.text)
        stats = indications.update(session, ids)
        _audit(session, request, user, "indication.decide", body.text[:120],
               {"code": body.code, "studies": len(ids)})
        session.commit()
        return {"studies": len(ids), "coded": dict(stats)}

    @app.post("/api/studies/{study_id}/his")
    def study_his_requery(study_id: int, request: Request, user: User = Depends(admin_user),
                          session: Session = Depends(db)):
        from ..his import enrich

        if session.get(Study, study_id) is None:
            raise HTTPException(404, "study not found")
        enrich.enqueue(session, [study_id], reset=True)
        _audit(session, request, user, "his.requery", f"study:{study_id}", None)
        session.commit()
        return data.his_summary(session, study_id)

    @app.post("/api/import/sizes")
    def import_sizes(request: Request, apply: bool = False,
                     body: bytes = Body(..., media_type="text/csv"),
                     user: User = Depends(admin_user), session: Session = Depends(db)):
        """Weight / height CSV (raw body). Dry run unless ?apply=1; see sizes_import."""
        from .. import sizes_import

        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("cp1250")
        try:
            res = sizes_import.run(session, text, apply=apply)
        except sizes_import.RowError as e:
            session.rollback()
            raise HTTPException(400, str(e)) from e
        if apply:
            _audit(session, request, user, "sizes.import", "csv", res["counts"])
            session.commit()
        else:
            session.rollback()
        return res

    @app.get("/api/studies/{study_id}/skin")
    def study_skin(study_id: int, user: User = Depends(current_user),
                   session: Session = Depends(db)):
        from .. import skindose

        r = skindose.for_study(session, study_id)
        if r is None:
            raise HTTPException(404, "study not found")
        return r

    @app.get("/api/quality")
    def quality_report(q: str = "origin:local date:90d", limit: int = 200,
                       user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import quality

        conds = data.checked_conditions(q, clock(), data.overrides(session))
        out = quality.run(session, conds, min(max(limit, 1), 1000),
                          names=data.device_names(session), physical=data._physical_ids(session))
        out["q"] = q
        return out

    @app.post("/api/studies/{study_id}/identify")
    def identify(study_id: int, request: Request, user: User = Depends(current_user),
                 session: Session = Depends(db)):
        if "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to see patient identifiers")
        out = data.identify(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
        _audit(session, request, user, "identify", f"study:{study_id}")
        session.commit()
        return out

    @app.get("/api/studies/{study_id}/dose-statement")
    def dose_statement(study_id: int, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        """The written dose information of art. 4. It names the patient: pid role, audited."""
        if "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to see patient identifiers")
        out = data.dose_statement(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
        _audit(session, request, user, "identify", f"dose-statement:study:{study_id}")
        session.commit()
        return out

    @app.get("/api/stats")
    def stats(q: str = "", metric: str = "dlp_total", by: str = "exam",
              system: str | None = None, exam: str | None = None,
              age_scheme: str = "ro-drl-age", weight_scheme: str = "ext-body-weight",
              bins: int = 0, min_n: int = 1, user: User = Depends(current_user),
              session: Session = Depends(db)):
        from .. import stats as st

        try:
            return data.stats(session, q, clock(), metric, by, system, exam, age_scheme,
                              weight_scheme, min(max(bins, 0), 100), max(min_n, 1))
        except st.StatsError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/stats/ct/acquisitions")
    def ct_acquisitions(q: str = "", user: User = Depends(current_user),
                        session: Session = Depends(db)):
        return data.ct_acquisitions(session, q, clock())

    @app.get("/api/stats/{group}/rows")
    def group_rows(group: str, q: str = "", user: User = Depends(current_user),
                   session: Session = Depends(db)):
        return data.group_rows(session, q, clock(), group)

    # ------------------------------------------------------------------ classification

    def _review(fn):
        try:
            return fn()
        except review.ReviewError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/classification/summary")
    def classification_summary(user: User = Depends(current_user),
                               session: Session = Depends(db)):
        return review.summary(session)

    @app.get("/api/classification/unclassified")
    def classification_unclassified(family: str = "rx", limit: int = 100,
                                    user: User = Depends(current_user),
                                    session: Session = Depends(db)):
        return _review(lambda: review.unclassified(session, family, min(max(limit, 1), 500)))

    @app.get("/api/classification/mappings")
    def classification_mappings(user: User = Depends(current_user),
                                session: Session = Depends(db)):
        return review.mappings(session)

    @app.put("/api/classification/mappings")
    def classification_decide(body: DecisionIn, request: Request,
                              user: User = Depends(admin_user),
                              session: Session = Depends(db)):
        out = _review(lambda: review.decide(session, body.family, body.field, body.text,
                                            body.code))
        _audit(session, request, user, "classification.decide",
               f"{body.family}:{body.field}", out)
        session.commit()
        return out

    @app.get("/api/codes/{system}")
    def code_list(system: str, user: User = Depends(current_user),
                  session: Session = Depends(db)):
        return _review(lambda: review.codes(session, system))

    # ------------------------------------------------------------------ users, audit

    def _users(fn):
        from . import users

        try:
            return fn(users)
        except users.UserError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/api/me/password")
    def my_password(body: PasswordIn, request: Request, user: User = Depends(current_user),
                    session: Session = Depends(db)):
        """Change one's own password (the account menu)."""
        u = session.get(User, user.id)
        _users(lambda m: m.change_own_password(u, body.current, body.new))
        _audit(session, request, u, "user.password", f"user:{u.username}")
        session.commit()
        return {"ok": True}

    @app.get("/api/users")
    def user_list(user: User = Depends(admin_user), session: Session = Depends(db)):
        from . import users

        return {"users": users.listing(session), "roles": users.ROLE_HELP}

    @app.post("/api/users")
    def user_create(body: UserIn, request: Request, user: User = Depends(admin_user),
                    session: Session = Depends(db)):
        from . import users

        u = _users(lambda m: m.create(session, body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "user.create", f"user:{u.username}",
               {"roles": u.roles, "name": u.display_name})
        session.commit()
        return users.as_dict(u)

    @app.patch("/api/users/{username}")
    def user_update(username: str, body: UserIn, request: Request,
                    user: User = Depends(admin_user), session: Session = Depends(db)):
        from . import users

        u, changes = _users(lambda m: m.update(session, user, username,
                                               body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "user.update", f"user:{u.username}", changes)
        session.commit()
        return {**users.as_dict(u), "changes": changes}

    @app.delete("/api/users/{username}")
    def user_delete(username: str, request: Request, user: User = Depends(admin_user),
                    session: Session = Depends(db)):
        gone = _users(lambda m: m.remove(session, user, username))
        _audit(session, request, user, "user.delete", f"user:{username}", gone)
        session.commit()
        return {"ok": True}

    @app.get("/api/audit")
    def audit_log(limit: int = 200, action: str | None = None, user_name: str | None = None,
                  text: str | None = None, since: str | None = None,
                  before_id: int | None = None, user: User = Depends(admin_user),
                  session: Session = Depends(db)):
        import datetime as _dt

        from . import users

        try:
            day = _dt.date.fromisoformat(since) if since else None
        except ValueError as e:
            raise HTTPException(400, "since is YYYY-MM-DD") from e
        return users.audit(session, min(max(limit, 1), 1000), action, user_name, text, day,
                           before_id)

    # ------------------------------------------------------------------ DICOM nodes

    def _nodes(fn):
        from ..net import nodes

        try:
            return fn(nodes)
        except nodes.NodeError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/dicom/status")
    def dicom_status(request: Request, user: User = Depends(current_user),
                     session: Session = Depends(db)):
        import datetime as _dt

        from ..net import nodes, qr

        nodes.import_config(session, settings)
        session.commit()
        today = clock().date()
        done = qr.done_days(session)
        days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(119, -1, -1)]
        return {
            "local": {"aet": settings.dicom_aet, "port": settings.dicom_port,
                      "bind": settings.dicom_bind, "ips": _host_ips(),
                      "web": f"{request.url.hostname}:{request.url.port or (443 if request.url.scheme == 'https' else 80)}",
                      "web_listen": f"{settings.web_host}:{settings.web_port}",
                      "allowed": nodes.allowed_senders(session, settings.dicom_allowed_aets)},
            "receiver": nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=120),
            "qr": nodes.read_status(session, nodes.QR_KEY, stale_after=300),
            "coverage": [{"date": d, "done": d in done} for d in days],
            "nodes": [nodes.as_dict(n) for n in session.scalars(
                select(DicomNode).order_by(DicomNode.name))],
        }

    @app.post("/api/dicom/nodes")
    def dicom_create(body: NodeIn, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        n = _nodes(lambda nodes: nodes.create(session, body.model_dump(exclude_unset=True)))
        from ..net.nodes import as_dict

        out = as_dict(n)
        _audit(session, request, user, "dicom_node.create", f"node:{n.name}",
               {k: v for k, v in out.items() if k != "last_echo"})
        session.commit()
        return out

    @app.patch("/api/dicom/nodes/{node_id}")
    def dicom_update(node_id: int, body: NodeIn, request: Request,
                     user: User = Depends(admin_user), session: Session = Depends(db)):
        n, changes = _nodes(lambda nodes: nodes.update(
            session, node_id, body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "dicom_node.update", f"node:{n.name}", changes)
        session.commit()
        return {"id": n.id, "changes": changes}

    @app.delete("/api/dicom/nodes/{node_id}")
    def dicom_delete(node_id: int, request: Request, user: User = Depends(admin_user),
                     session: Session = Depends(db)):
        d = _nodes(lambda nodes: nodes.delete(session, node_id))
        _audit(session, request, user, "dicom_node.delete", f"node:{d['name']}", d)
        session.commit()
        return {"deleted": d["name"]}

    @app.post("/api/dicom/nodes/{node_id}/echo")
    def dicom_echo(node_id: int, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        res = _nodes(lambda nodes: nodes.echo(session, node_id, settings.dicom_aet))
        session.commit()
        return res

    # ------------------------------------------------------------------ HIS servers

    def _his(fn):
        from ..his import servers

        try:
            return fn(servers)
        except servers.ServerError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/his/status")
    def his_status(user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import AppSetting, HisLookup, HisServer
        from ..his import servers

        row = session.get(AppSetting, servers.STATUS_KEY)
        worker = dict(row.value or {}) if row else None
        if worker and worker.get("at"):
            age = (clock() - dt.datetime.fromisoformat(worker["at"])).total_seconds()
            worker["stale"] = age > 600
        queue = dict(session.execute(select(HisLookup.status, func.count())
                                     .group_by(HisLookup.status)).all())
        return {"worker": worker, "queue": queue,
                "servers": [servers.as_dict(s) for s in session.scalars(
                    select(HisServer).order_by(HisServer.name))]}

    # ------------------------------------------------------------------ live (SSE)
    # The receiver, PACS sync and HIS worker are separate processes that only meet the web
    # server in the database, so the stream reads a small snapshot every LIVE_EVERY_S and
    # sends a topic only when it changed. One open tab = one idle async task, no thread.

    def study_stamp(session, sid: int) -> dict | None:
        """A fingerprint of one study: its row, events, alerts, HIS outcome and patient."""
        from ..db.models import Alert, Event, HisLookup, Patient, Study

        st = session.get(Study, sid)
        if st is None:
            return None
        ev = session.execute(select(func.count(Event.id), func.max(Event.id))
                             .where(Event.study_id == sid)).one()
        al = session.execute(select(Alert.id, Alert.status).where(Alert.study_id == sid)
                             .order_by(Alert.id)).all()
        his = session.get(HisLookup, sid)
        pat = session.get(Patient, st.patient_id) if st.patient_id else None
        return {"at": st.updated_at, "events": list(ev), "alerts": [list(a) for a in al],
                "his": [his.status, his.fetched_at] if his else None,
                "patient": pat.updated_at if pat else None}

    def live_snapshot(topics: set[str]) -> dict:
        from ..db.models import AppSetting, HisLookup, Study
        from ..his import servers
        from ..net import nodes

        out: dict = {}
        with maker() as session:
            if "pulse" in topics:  # cheap: pages refetch their own data when it moves
                from .. import alerts as _alerts
                from ..db.models import Alert

                out["pulse"] = {"study": session.scalar(select(func.max(Study.id))),
                                "alert": session.scalar(select(func.max(Alert.id))),
                                "alerts_open": _alerts.open_count(session)}
            if "services" in topics:
                out["services"] = services_health(session)
            if "studies" in topics:
                # per modality, from the latest 2000 studies (PK scan): the newest id and the
                # last change (more images, HIS details, a new classification)
                newest: dict[str, list] = {}
                for sid, mod, upd in session.execute(
                        select(Study.id, Study.modality, Study.updated_at)
                        .order_by(Study.id.desc()).limit(2000)):
                    cur = newest.setdefault(mod or "?", [sid, upd])
                    if upd and (cur[1] is None or upd > cur[1]):
                        cur[1] = upd
                out["studies"] = newest
            for t in topics:  # "study:<id>": what an open study page shows changed
                if t.startswith("study:"):
                    out[t] = study_stamp(session, int(t[6:]))
            if "alerts" in topics:
                from .. import alerts as _alerts
                from ..db.models import Alert

                out["alerts"] = {"open": _alerts.open_count(session),
                                 "newest": session.scalar(select(func.max(Alert.id)))}
            if "dicom" in topics:
                out["dicom"] = {
                    "receiver": nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=120),
                    "qr": nodes.read_status(session, nodes.QR_KEY, stale_after=300)}
            if "his" in topics:
                row = session.get(AppSetting, servers.STATUS_KEY)
                worker = dict(row.value or {}) if row else None
                if worker and worker.get("at"):
                    age = (clock() - dt.datetime.fromisoformat(worker["at"])).total_seconds()
                    worker["stale"] = age > 600
                queue = dict(session.execute(select(HisLookup.status, func.count())
                                             .group_by(HisLookup.status)).all())
                recent = session.execute(
                    select(HisLookup, Study.accession, Study.accession_is_hashed,
                           Study.study_datetime)
                    .join(Study, Study.id == HisLookup.study_id)
                    .where(HisLookup.fetched_at.is_not(None))
                    .order_by(HisLookup.fetched_at.desc()).limit(20)).all()
                out["his"] = {"worker": worker, "queue": queue, "recent": [{
                    "study_id": lk.study_id, "status": lk.status,
                    "at": lk.fetched_at.isoformat(timespec="seconds"),
                    "study_at": when.isoformat(timespec="minutes") if when else None,
                    "accession": None if hashed else acc, "request_id": lk.request_id,
                    "score": lk.match_score, "how": (lk.detail or {}).get("type"),
                    "written": (lk.detail or {}).get("written"),
                    "next_try": lk.next_try_at.isoformat(timespec="minutes")
                    if lk.status == "pending" and lk.next_try_at else None}
                    for lk, acc, hashed, when in recent]}
        return out

    def services_health(session: Session) -> list[dict]:
        """The background services, for any signed-in user: state ok | warn | alert | idle."""
        from .. import __version__
        from ..db.models import AppSetting, HisLookup
        from ..his import servers
        from ..net import nodes

        out = [{"key": "web", "name": "Web server", "state": "ok", "detail": f"v{__version__}"}]
        rx = nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=120)
        spool = (rx or {}).get("spool") or {}
        out.append({"key": "receiver", "name": "DICOM receiver",
                    "state": "alert" if not rx or not rx["running"] else
                    "warn" if spool.get("derive_gave_up") else "ok",
                    "at": rx and rx["at"], "age_s": rx and rx["age_s"],
                    "detail": "never started" if not rx else
                    f"{rx.get('aet')} :{rx.get('port')} · {rx.get('received', 0)} received"
                    + (f" · {spool['derive_pending']} to process" if spool.get("derive_pending")
                       else "")})
        qr = nodes.read_status(session, nodes.QR_KEY, stale_after=300)
        out.append({"key": "qr", "name": "PACS sync",
                    "state": "ok" if qr and qr["running"] else "idle",
                    "at": qr and qr["at"], "age_s": qr and qr["age_s"],
                    "detail": _qr_detail(qr)})
        if servers.active(session):
            row = session.get(AppSetting, servers.STATUS_KEY)
            w = dict(row.value or {}) if row else {}
            age = int((clock() - dt.datetime.fromisoformat(w["at"])).total_seconds()) \
                if w.get("at") else None
            waiting = session.scalar(select(func.count()).select_from(HisLookup)
                                     .where(HisLookup.status == "pending")) or 0
            out.append({"key": "his", "name": "HIS worker",
                        "state": "alert" if w.get("error") or age is None or age > 600 else "ok",
                        "at": w.get("at"), "age_s": age,
                        "detail": w.get("error") or (f"{waiting} waiting" if waiting else "queue empty")})
        else:
            out.append({"key": "his", "name": "HIS worker", "state": "idle",
                        "detail": "no HIS server enabled"})
        return out

    def _qr_detail(qr: dict | None) -> str:
        """'2013-01-17: {…counters…}; imported {…}' -> 'at 2013-01-17'; other messages as is."""
        msg = (qr or {}).get("message") or ""
        if not msg:
            return "not run yet"
        m = re.match(r"(\d{4}-\d{2}-\d{2}):", msg)
        return f"{'at' if qr['running'] else 'last'} {m.group(1)}" if m else msg[:80]

    @app.get("/api/live")
    async def live(request: Request, topics: str = "pulse"):
        import asyncio
        import json as _json

        from starlette.concurrency import run_in_threadpool

        def who():
            with maker() as session:
                return current_user(request, session)

        user = await run_in_threadpool(who)  # 401 before the stream opens
        want = {t for t in topics.split(",")
                if t in LIVE_TOPICS or re.fullmatch(r"study:\d{1,12}", t)}
        if "his" in want and "admin" not in user.role_set():
            want.discard("his")

        async def stream():
            last: dict = {}
            quiet = elapsed = 0.0
            yield f"retry: {LIVE_RETRY_MS}\n\n"
            due_at: dict[str, float] = {}
            while True:
                now_t = elapsed
                topics_now = {t for t in want if due_at.get(t, 0.0) <= now_t}
                for t in topics_now & LIVE_TOPIC_EVERY_S.keys():
                    due_at[t] = now_t + LIVE_TOPIC_EVERY_S[t]
                snap = await run_in_threadpool(live_snapshot, topics_now) if topics_now else {}
                for topic, value in snap.items():
                    # ages tick every second: compare without them, send them along
                    key = _json.dumps(value, sort_keys=True, default=str,
                                      separators=(",", ":"))
                    key = re.sub(r'"age_s":\d+,?', "", key)
                    if last.get(topic) != key:
                        last[topic] = key
                        quiet = 0.0
                        yield f"event: {topic}\ndata: {_json.dumps(value, default=str)}\n\n"
                if elapsed >= LIVE_MAX_S or await request.is_disconnected():
                    return  # the browser reconnects after `retry`, with a fresh state
                await asyncio.sleep(LIVE_EVERY_S)
                quiet += LIVE_EVERY_S
                elapsed += LIVE_EVERY_S
                if quiet >= LIVE_PING_S:  # keep proxies from closing an idle stream
                    quiet = 0.0
                    yield ": ping\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/his/servers")
    def his_create(body: HisServerIn, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        s = _his(lambda sv: sv.create(session, secret, body.model_dump(exclude_unset=True)))
        from ..his.servers import as_dict

        out = as_dict(s)
        _audit(session, request, user, "his_server.create", f"his:{s.name}",
               {k: v for k, v in out.items() if k != "last_check"})
        session.commit()
        return out

    @app.patch("/api/his/servers/{server_id}")
    def his_update(server_id: int, body: HisServerIn, request: Request,
                   user: User = Depends(admin_user), session: Session = Depends(db)):
        s, changes = _his(lambda sv: sv.update(session, secret, server_id,
                                               body.model_dump(exclude_unset=True)))
        _audit(session, request, user, "his_server.update", f"his:{s.name}", changes)
        session.commit()
        return {"id": s.id, "changes": changes}

    @app.delete("/api/his/servers/{server_id}")
    def his_delete(server_id: int, request: Request, user: User = Depends(admin_user),
                   session: Session = Depends(db)):
        d = _his(lambda sv: sv.delete(session, server_id))
        _audit(session, request, user, "his_server.delete", f"his:{d['name']}", d)
        session.commit()
        return {"deleted": d["name"]}

    @app.post("/api/his/servers/{server_id}/check")
    def his_check(server_id: int, user: User = Depends(admin_user),
                  session: Session = Depends(db)):
        res = _his(lambda sv: sv.check(session, secret, server_id))
        session.commit()
        return res

    # ------------------------------------------------------------------ alerts

    def _alert_filters(rule, level, since, until, modality):
        import datetime as _dt

        try:
            return {"rule": rule or None, "level": level or None, "modality": modality or None,
                    "since": _dt.datetime.fromisoformat(since) if since else None,
                    "until": (_dt.datetime.fromisoformat(until) + _dt.timedelta(days=1))
                    if until else None}
        except ValueError as e:
            raise HTTPException(400, "since/until are YYYY-MM-DD") from e

    @app.get("/api/alerts")
    def alert_list(status: str = "open", limit: int = 50, offset: int = 0,
                   rule: str | None = None, level: str | None = None,
                   since: str | None = None, until: str | None = None,
                   modality: str | None = None, user: User = Depends(current_user),
                   session: Session = Depends(db)):
        from .. import alerts

        return alerts.listing(session, None if status == "all" else status,
                              min(max(limit, 1), 500), names=data.device_names(session),
                              offset=max(offset, 0),
                              **_alert_filters(rule, level, since, until, modality))

    @app.get("/api/alerts/summary")
    def alert_summary(status: str = "open", rule: str | None = None, level: str | None = None,
                      since: str | None = None, until: str | None = None,
                      modality: str | None = None, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from .. import alerts

        return alerts.counts(session, None if status == "all" else status,
                             **_alert_filters(rule, level, since, until, modality))

    @app.post("/api/alerts/ack")
    def alert_ack_many(body: AckManyIn, request: Request, user: User = Depends(current_user),
                       session: Session = Depends(db)):
        from .. import alerts

        if not body.ids or len(body.ids) > 1000:
            raise HTTPException(400, "1 to 1000 alert ids")
        n = alerts.acknowledge(session, body.ids, user.username, body.note, clock())
        _audit(session, request, user, "alert.ack", f"alerts:{n}",
               {"ids": body.ids[:50], "note": body.note})
        session.commit()
        return {"acknowledged": n}

    @app.get("/api/alerts/{alert_id}/studies")
    def alert_studies(alert_id: int, user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from .. import alerts

        rows = alerts.window_studies(session, alert_id, names=data.device_names(session))
        if rows is None:
            raise HTTPException(404, "alert not found")
        return rows

    @app.post("/api/alerts/{alert_id}/ack")
    def alert_ack(alert_id: int, body: AckIn, request: Request,
                  user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import Alert

        a = session.get(Alert, alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        a.status, a.ack_by, a.ack_at = "acknowledged", user.username, clock()
        a.ack_note = (body.note or "")[:1024] or None
        _audit(session, request, user, "alert.ack", f"alert:{alert_id}",
               {"study_id": a.study_id, "note": a.ack_note})
        session.commit()
        return {"id": a.id, "status": a.status, "ack_by": a.ack_by}

    @app.get("/api/alert-rules")
    def alert_rules(user: User = Depends(current_user), session: Session = Depends(db)):
        from ..db.models import AlertRule

        cols = ("id", "key", "name", "enabled", "kind", "modality", "metric", "threshold",
                "unit", "window_days", "max_age_days", "severity", "notify", "note")
        return [{c: getattr(r, c) for c in cols}
                for r in session.scalars(select(AlertRule).order_by(AlertRule.id))]

    @app.patch("/api/alert-rules/{rule_id}")
    def alert_rule_patch(rule_id: int, body: RulePatch, request: Request,
                         user: User = Depends(admin_user), session: Session = Depends(db)):
        from ..db.models import AlertRule

        r = session.get(AlertRule, rule_id)
        if r is None:
            raise HTTPException(404, "rule not found")
        patch = body.model_dump(exclude_unset=True)
        if "severity" in patch and patch["severity"] not in ("warn", "alert"):
            raise HTTPException(400, "severity is warn|alert")
        changes = {k: {"from": getattr(r, k), "to": v} for k, v in patch.items()
                   if getattr(r, k) != v}
        for k, v in patch.items():
            setattr(r, k, v)
        _audit(session, request, user, "alert_rule.update", f"rule:{r.key}", changes)
        session.commit()
        return {"id": r.id, "changes": changes}

    # ------------------------------------------------------------------ exports

    @app.get("/api/export")
    def export(request: Request, q: str = "", level: str = "study", format: str = "csv",
               pid: bool = False, user: User = Depends(current_user),
               session: Session = Depends(db)):
        import tempfile

        from ..export import lists

        roles = user.role_set()
        if not roles & {"exporter", "admin"}:
            raise HTTPException(403, "the exporter role is required")
        if pid and "pid" not in roles:
            raise HTTPException(403, "the pid role is required to export patient identifiers")
        if level not in ("study", "event") or format not in ("csv", "xlsx"):
            raise HTTPException(400, "level is study|event, format is csv|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        names, physical = data.device_names(session), data._physical_ids(session)
        _audit(session, request, user, "export", f"{level}:{format}",
               {"q": q, "pid": pid})
        session.commit()
        fname = f"dozimetrix-{level}s-{now:%Y%m%d-%H%M}.{format}"
        headers = {"Content-Disposition": f'attachment; filename="{fname}"',
                   "Cache-Control": "no-store"}

        def source(s):
            return lists.rows(s, conds, level, pid, device_names=names, physical=physical)

        if format == "xlsx":
            # closed by the streaming generator once the file has been sent
            tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115
            with maker() as s:
                lists.write_xlsx(tmp, level, pid, source(s), meta=[
                    ("Exported", now.isoformat(sep=" ", timespec="seconds")),
                    ("By", user.username), ("Query", q or "(all)"), ("Level", level),
                    ("Patient identifiers", "yes" if pid else "no (pseudonyms)")])
            tmp.seek(0)

            def chunks():
                with tmp:
                    while block := tmp.read(1 << 16):
                        yield block

            return StreamingResponse(
                chunks(), headers=headers,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        def csv_stream():
            import csv
            import io

            with maker() as s:
                buf = io.StringIO()
                w = csv.writer(buf, lineterminator="\r\n")
                buf.write("\ufeff")  # BOM: Excel opens UTF-8 CSV with diacritics intact
                w.writerow([h for h, _ in lists.columns(level, pid)])
                for i, row in enumerate(source(s)):
                    w.writerow([lists._csv_value(v) for v in row])
                    if i % 500 == 499:
                        yield buf.getvalue().encode("utf-8")
                        buf.seek(0)
                        buf.truncate()
                yield buf.getvalue().encode("utf-8")

        return StreamingResponse(csv_stream(), headers=headers, media_type="text/csv")

    def _exporter(user: User) -> None:
        if not user.role_set() & {"exporter", "admin"}:
            raise HTTPException(403, "the exporter role is required")

    def _file(write, fmt: str, stem: str, now: dt.datetime):
        """Run ``write(fh)`` into a temporary file, then stream it as a download."""
        import tempfile

        media = {"csv": "text/csv", "json": "application/json", "dcm": "application/dicom",
                 "zip": "application/zip",
                 "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
        # closed by the streaming generator once the file has been sent
        tmp = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)  # noqa: SIM115
        write(tmp)
        tmp.seek(0)

        def chunks():
            with tmp:
                while block := tmp.read(1 << 16):
                    yield block

        fname = f"{stem}-{now:%Y%m%d-%H%M}.{fmt}"
        return StreamingResponse(chunks(), media_type=media[fmt], headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store"})

    @app.get("/api/studies/{study_id}/rdsr")
    def study_rdsr(study_id: int, request: Request, pid: bool = False,
                   user: User = Depends(current_user), session: Session = Depends(db)):
        """The study's dose SR (IHE REM; RRDSR for NM/PET) as a download; pseudonymised unless
        pid=1 (pid role).
        A file download only: Dozimetrix does not send dose SRs anywhere."""
        from ..export import rdsr

        _exporter(user)
        if pid and "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required to export patient identifiers")
        st = session.get(Study, study_id)
        if st is None:
            raise HTTPException(404, "study not found")
        if not st.events:
            raise HTTPException(400, "the study has no dose events")
        ds = rdsr.build(st, app.state.secret, identified=pid)
        _audit(session, request, user, "export", f"rdsr:study:{study_id}", {"pid": pid})
        session.commit()
        return _file(lambda fh: rdsr.write(ds, fh), "dcm", f"rdsr-{study_id}", clock())

    @app.get("/api/drl/dataset")
    def drl_dataset(request: Request, q: str = "origin:local", format: str = "xlsx",
                    user: User = Depends(current_user), session: Session = Depends(db)):
        from ..export import drl_dataset as ds

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        _audit(session, request, user, "export", f"drl-dataset:{format}", {"q": q})
        session.commit()

        def write(fh):
            with maker() as s:
                ds.write(format, fh, ds.dataset_rows(s, conds, secret), ds.metadata(q, now))

        return _file(write, format, "dozimetrix-drl-dataset", now)

    @app.get("/api/drl/summary")
    def drl_summary(request: Request, q: str = "origin:local", set: str = "ro-ms838-2025",
                    min_n: int = 20, format: str = "xlsx", user: User = Depends(current_user),
                    session: Session = Depends(db)):
        from .. import drl as drl_mod
        from ..export import drl_dataset as ds
        from ..export.xlsx import XlsxWriter

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        try:
            rows = ds.summary_rows(session, conds, set, max(min_n, 1))
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "export", f"drl-summary:{format}",
               {"q": q, "set": set, "min_n": min_n})
        session.commit()
        meta = {**ds.metadata(q, now, len(rows)), "dataset": "dozimetrix-icrp135-summary",
                "reference_set": set, "min_n": min_n}

        def write(fh):
            if format == "json":
                fh.write(json.dumps({**meta, "rows": rows}, ensure_ascii=False,
                                    indent=1).encode("utf-8"))
            elif format == "csv":
                import csv
                import io

                text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
                w = csv.DictWriter(text, ds.SUMMARY_FIELDS, lineterminator="\r\n")
                w.writeheader()
                from ..export.lists import safe_text

                w.writerows({k: ("" if v is None else safe_text(v)) for k, v in r.items()}
                            for r in rows)
                text.flush()
                text.detach()
            else:
                with XlsxWriter(fh) as wb:
                    ws = wb.sheet("ICRP 135 summary", ds.SUMMARY_FIELDS,
                                  [9, 18, 18, 30, 10, 9, 10] + [9] * 13)
                    for r in rows:
                        ws.row([r[f] for f in ds.SUMMARY_FIELDS])
                    about = wb.sheet("About", ["Item", "Value"], [16, 90])
                    for k, v in meta.items():
                        about.row([k, v])

        return _file(write, format, "dozimetrix-icrp135-summary", now)

    @app.get("/api/survey/export")
    def survey_export(request: Request, month: str, q: str = "origin:local",
                      format: str = "csv", user: User = Depends(current_user),
                      session: Session = Depends(db)):
        from ..export import drl_dataset as ds
        from ..export import survey

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        try:
            start, end = survey.month_range(month)
        except ValueError as e:
            raise HTTPException(400, "month is YYYY-MM") from e
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        _audit(session, request, user, "export", f"survey:{month}:{format}", {"q": q})
        session.commit()
        meta = {**ds.metadata(q, now), "dataset": "dozimetrix-paediatric-survey",
                "version": survey.SURVEY_VERSION, "month": month,
                "plan": "MS-CNCAN-SDNP-2026-001 draft v1.0 (March 2026)"}

        def write(fh):
            with maker() as s:
                ds.write(format, fh, survey.rows(s, conds, secret), meta, survey.DICTIONARY)

        return _file(write, format, f"dozimetrix-survey-{month}", now)

    # ------------------------------------------------------------------ central survey

    def _central(user: User) -> None:
        if not user.role_set() & {"admin", "importer"}:
            raise HTTPException(403, "the importer or admin role is required")

    def _agg(fn):
        from .. import aggregate

        try:
            return fn(aggregate)
        except aggregate.SurveyError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/survey/uploads")
    def survey_upload(request: Request, filename: str | None = None,
                      body: bytes = Body(..., media_type="text/csv"),
                      user: User = Depends(current_user), session: Session = Depends(db)):
        """A site's survey export (CSV or JSON) into the central collection."""
        _central(user)
        try:
            up = _agg(lambda a: a.upload(session, body, filename=filename, user=user.username))
        except HTTPException:
            session.rollback()
            raise
        _audit(session, request, user, "survey.upload", f"{up.institution}:{up.period}",
               {"rows": up.n_rows, "rejected": up.n_rejected, "file": filename})
        session.commit()
        return {"id": up.id, "institution": up.institution, "period": up.period,
                "rows": up.n_rows, "rejected": up.n_rejected, "report": up.report}

    @app.get("/api/survey/uploads")
    def survey_uploads(user: User = Depends(current_user), session: Session = Depends(db)):
        return _agg(lambda a: a.uploads(session))

    @app.delete("/api/survey/uploads/{upload_id}")
    def survey_upload_delete(upload_id: int, request: Request,
                             user: User = Depends(admin_user), session: Session = Depends(db)):
        out = _agg(lambda a: a.remove(session, upload_id))
        _audit(session, request, user, "survey.delete", f"upload:{upload_id}", out)
        session.commit()
        return out

    @app.put("/api/survey/institutions/{code}")
    def survey_institution(code: str, body: InstitutionNameIn, request: Request,
                           user: User = Depends(admin_user), session: Session = Depends(db)):
        _agg(lambda a: a.name_institution(session, code, body.name))
        _audit(session, request, user, "survey.institution", code, {"name": body.name})
        session.commit()
        return {"code": code, "name": body.name}

    @app.get("/api/survey/national")
    def survey_national(request: Request, start: str, end: str, grid: str = "rp185",
                        estimated: bool = False, format: str = "json",
                        user: User = Depends(current_user), session: Session = Depends(db)):
        """National statistics over the uploaded exports (P75 of institution medians)."""
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        rep = _agg(lambda a: a.national(session, start, end, grid=grid,
                                        estimated_weights=estimated))
        if format == "json":
            return rep
        _exporter(user)
        _audit(session, request, user, "report", f"survey.national:{start}..{end}",
               {"grid": grid})
        session.commit()
        from .. import aggregate

        info = {"Generat": clock().isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username}
        return _file(lambda fh: aggregate.write_xlsx(rep, fh, info), "xlsx",
                     f"studiu-national-{start}-{end}", clock())

    @app.get("/api/reports/drl-trend")
    def report_drl_trend(request: Request, year: int | None = None, quarter: int | None = None,
                         quarters: int = 6, set: str = "ro-ms838-2025",
                         q: str = "origin:local", format: str = "json",
                         user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod
        from ..reports import trend

        _exporter(user)
        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 2 <= quarters <= 12:
            raise HTTPException(400, "quarter is 1..4, quarters is 2..12")
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        try:
            report = trend.build(session, conds, year, quarter, quarters, set)
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "report", f"drl-trend:{year}Q{quarter}:{format}",
               {"q": q, "set": set, "quarters": quarters})
        session.commit()
        if format == "json":
            return report
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: trend.write_xlsx(report, fh, info), "xlsx",
                     f"drl-trend-{year}-T{quarter}", now)

    @app.get("/api/reports/operators")
    def report_operators(request: Request, year: int | None = None, quarter: int | None = None,
                         quarters: int = 4, q: str = "origin:local", min_n: int = 20,
                         format: str = "json", user: User = Depends(admin_user),
                         session: Session = Depends(db)):
        """Doses per operator against the department median (exam, age group). Staff names:
        admin only, audited."""
        from ..reports import operators, trend
        from ..reports.insp1255 import quarter_range

        if year is None or quarter is None:
            year, quarter = trend.default_end(clock().date())
        if quarter not in (1, 2, 3, 4) or not 1 <= quarters <= 8:
            raise HTTPException(400, "quarter is 1..4, quarters is 1..8")
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        first = trend.quarters_back(year, quarter, quarters)[0]
        start, _ = quarter_range(*first)
        _, end = quarter_range(year, quarter)
        now = clock()
        conds = [*data.checked_conditions(q, now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        rep = operators.build(session, conds, start.date(), (end - dt.timedelta(days=1)).date(),
                              max(min_n, 1))
        _audit(session, request, user, "report", f"operators:{year}Q{quarter}:{format}",
               {"q": q, "quarters": quarters})
        session.commit()
        if format == "json":
            return rep
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: operators.write_xlsx(rep, fh, info), "xlsx",
                     f"operatori-{year}-T{quarter}", now)

    @app.get("/api/reports/insp1255")
    def report_insp1255(request: Request, year: int, quarter: int, q: str = "origin:local",
                        format: str = "json", user: User = Depends(current_user),
                        session: Session = Depends(db)):
        from ..reports import insp1255

        _exporter(user)
        try:
            start, end = insp1255.quarter_range(year, quarter)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        report = insp1255.build(session, conds, start, end)
        _audit(session, request, user, "report", f"insp1255:{year}Q{quarter}:{format}",
               {"q": q})
        session.commit()
        if format == "json":
            return insp1255.as_json(report)
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: insp1255.write_xlsx(report, fh, info), "xlsx",
                     f"insp-1255-{year}-T{quarter}", now)

    from . import exports as _exports

    _exports.register(app, db=db, current_user=current_user, clock=clock, file_response=_file,
                      audit=_audit, settings=settings, secret=secret)

    @app.get("/api/reports/art12")
    def report_art12(request: Request, year: int, q: str = "origin:local", format: str = "json",
                     user: User = Depends(current_user), session: Session = Depends(db)):
        """Annual DRL return (art. 12): the summary as JSON, or the zip of workbooks. The
        workbooks carry the first 7 CNP digits and the indication, so they need ``pid``."""
        from ..reports import art12

        _exporter(user)
        if format not in ("json", "zip"):
            raise HTTPException(400, "format is json|zip")
        if format == "zip" and "pid" not in user.role_set():
            raise HTTPException(403, "the pid role is required: the workbooks hold patient "
                                     "identifiers")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        books = art12.build(session, year, conds)
        _audit(session, request, user, "report", f"art12:{year}:{format}",
               {"q": q, "pid": format == "zip"})
        session.commit()
        if format == "json":
            return {"year": year, "per_sheet": art12.PATIENTS_PER_SHEET,
                    "window": [art12.window(year)[0].date().isoformat(),
                               (art12.window(year)[1] - dt.timedelta(days=1)).date().isoformat()],
                    "workbooks": art12.summary(books)}
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: art12.write_zip(books, fh, info), "zip", f"art12-{year}", now)

    @app.get("/api/reports/grid")
    def report_grid(request: Request, year: int, quarter: int, q: str = "origin:local",
                    format: str = "json", min_n: int = 10, estimated: bool = False,
                    user: User = Depends(current_user), session: Session = Depends(db)):
        """The second grid: ICRP 135 / RP 185 groups (head by age, body by weight)."""
        from ..reports import grid
        from ..reports.insp1255 import quarter_range

        _exporter(user)
        try:
            start, end = quarter_range(year, quarter)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if format not in ("json", "xlsx"):
            raise HTTPException(400, "format is json|xlsx")
        now = clock()
        conds = data.checked_conditions(q, now, data.overrides(session))
        report = grid.build(session, conds, start, end, max(min_n, 1), estimated)
        _audit(session, request, user, "report", f"grid:{year}Q{quarter}:{format}",
               {"q": q, "estimated": estimated})
        session.commit()
        if format == "json":
            return report
        info = {"Generat": now.isoformat(sep=" ", timespec="seconds"),
                "De": user.display_name or user.username, "Selecție": q or "(toate)"}
        return _file(lambda fh: grid.write_xlsx(report, fh, info), "xlsx",
                     f"grila-icrp135-{year}-T{quarter}", now)

    @app.get("/api/lookup")
    def lookup(q: str = "", limit: int = 6, user: User = Depends(current_user),
               session: Session = Depends(db)):
        return data.lookup(session, q, min(max(limit, 1), 20))

    # ------------------------------------------------------------------ pages

    @app.get("/login", response_class=HTMLResponse)
    def login_page(session: Session = Depends(db)):
        if first_run.needs_admin(session):
            return RedirectResponse("/setup", status_code=303)
        return FileResponse(LOGIN_HTML)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, session: Session = Depends(db)):
        if not first_run.needs_admin(session):
            user = optional_user(request, session)
            if user is None:
                return RedirectResponse("/login", status_code=303)
            if "admin" not in user.role_set():
                raise HTTPException(403, "the admin role is required")
        return FileResponse(SETUP_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/{page:path}", response_class=HTMLResponse)
    def pages(page: str, request: Request, session: Session = Depends(db)):
        if page in OLD_PAGES:  # renamed pages: old bookmarks keep working
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/{OLD_PAGES[page]}{query}", status_code=301)
        if page.startswith(("assets/", "api/")) or page not in PAGES:
            raise HTTPException(404)
        uid = read_session(secret, request.cookies.get(COOKIE))
        if not uid or session.get(User, uid) is None:
            return RedirectResponse("/setup" if first_run.needs_admin(session) else "/login",
                                    status_code=303)
        html = (WEB / PAGES[page]).read_text(encoding="utf-8")
        origin = str(request.base_url).rstrip("/")
        html = re.sub(r"<html(\s|>)", f'<html data-api="{origin}"\\1', html, count=1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/assets", RevalidatedStatic(directory=WEB / "assets"), name="assets")

    @app.exception_handler(data.QueryError)
    def bad_query(request: Request, exc: data.QueryError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(HTTPException)
    def http_error(request: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # route order: pages catch-all must come after the API; FastAPI matches in order of
    # registration, and the static mount is checked before routes for /assets.
    app.router.routes.sort(key=lambda r: getattr(r, "path", "") == "/{page:path}")
    return app


def _me(user: User) -> dict:
    return {"username": user.username, "name": user.display_name,
            "roles": sorted(user.role_set())}


def _audit(session: Session, request: Request, user: User | None, action: str,
           target: str | None, detail: dict | None = None) -> None:
    session.add(AuditLog(user_id=user.id if user else None,
                         username=user.username if user else None, action=action,
                         target=target, detail=detail,
                         remote_addr=request.client.host if request.client else None))
