"""FastAPI application: serves the static UI and the JSON API.

Privacy rules:
* every /api route except /api/login requires a session;
* responses carry patient pseudonyms only; identifiers need the `pid` role and an explicit,
  audit-logged call (POST /api/studies/{id}/identify);
* the server is meant to bind to localhost or sit behind an HTTPS reverse proxy, and the
  access log is disabled by `dosetrail serve` because query strings may contain identifiers.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import Settings, load_settings
from ..db.models import AuditLog, User
from ..db.session import make_engine, make_sessionmaker
from ..security import load_or_create_secret, read_session, sign_session, verify_password
from . import admin, data, review

WEB = Path(__file__).resolve().parent.parent / "web"
LOGIN_HTML = Path(__file__).resolve().parent / "login.html"
COOKIE = "dt_session"
PAGES = {"": "index.html", "index.html": "index.html", "studies.html": "studies.html",
         "study.html": "study.html", "charts.html": "charts.html",
         "devices.html": "devices.html"}


class LoginIn(BaseModel):
    username: str
    password: str


class DevicePatch(BaseModel):
    display_name: str | None = None
    modality_override: str | None = None
    origin: str | None = None


class MergeIn(BaseModel):
    record_ids: list[int]
    survivor_id: int
    display_name: str | None = None


class DismissIn(BaseModel):
    record_ids: list[int]


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
    app = FastAPI(title="DoseTrail", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.secret = secret
    app.state.settings = settings
    app.state.fixed_now = now  # tests only

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

    # ------------------------------------------------------------------ data

    @app.get("/api/overview")
    def overview(user: User = Depends(current_user), session: Session = Depends(db)):
        return data.overview(session, clock())

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

    @app.get("/api/studies")
    def studies(q: str = "", sort: str = "date", dir: str = "desc", page: int = 1,
                size: int = 50, user: User = Depends(current_user),
                session: Session = Depends(db)):
        return data.studies(session, q, sort, dir, max(page, 1), min(max(size, 1), 500), clock())

    @app.get("/api/studies/{study_id}")
    def study(study_id: int, user: User = Depends(current_user),
              session: Session = Depends(db)):
        out = data.study(session, study_id)
        if out is None:
            raise HTTPException(404, "study not found")
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

    @app.get("/api/stats/ct/acquisitions")
    def ct_acquisitions(q: str = "", user: User = Depends(current_user),
                        session: Session = Depends(db)):
        return data.ct_acquisitions(session, q, clock())

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

    @app.get("/api/lookup")
    def lookup(q: str = "", limit: int = 6, user: User = Depends(current_user),
               session: Session = Depends(db)):
        return data.lookup(session, q, min(max(limit, 1), 20))

    # ------------------------------------------------------------------ pages

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        return FileResponse(LOGIN_HTML)

    @app.get("/{page:path}", response_class=HTMLResponse)
    def pages(page: str, request: Request, session: Session = Depends(db)):
        if page.startswith(("assets/", "api/")) or page not in PAGES:
            raise HTTPException(404)
        uid = read_session(secret, request.cookies.get(COOKIE))
        if not uid or session.get(User, uid) is None:
            return RedirectResponse("/login", status_code=303)
        html = (WEB / PAGES[page]).read_text(encoding="utf-8")
        origin = str(request.base_url).rstrip("/")
        html = re.sub(r"<html(\s|>)", f'<html data-api="{origin}"\\1', html, count=1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

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
