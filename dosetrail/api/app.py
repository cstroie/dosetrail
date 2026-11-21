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
import json
import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..config import Settings, load_settings
from ..db.models import AuditLog, DicomNode, Study, User
from ..db.session import make_engine, make_sessionmaker
from ..security import load_or_create_secret, read_session, sign_session, verify_password
from . import admin, data, review

WEB = Path(__file__).resolve().parent.parent / "web"
LOGIN_HTML = Path(__file__).resolve().parent / "login.html"
COOKIE = "dt_session"
PAGES = {"": "index.html", "index.html": "index.html", "studies.html": "studies.html",
         "study.html": "study.html", "charts.html": "charts.html",
         "devices.html": "devices.html", "drl.html": "drl.html", "review.html": "review.html",
         "reports.html": "reports.html", "quality.html": "quality.html",
         "rules.html": "rules.html", "dicom.html": "dicom.html"}


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


class AckIn(BaseModel):
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

    @app.get("/api/drl/sets")
    def drl_sets(user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import drl as drl_mod

        return drl_mod.reference_sets(session)

    @app.get("/api/drl/compare")
    def drl_compare(q: str = "origin:local", set: str = "ro-ms838-2025", by: str = "facility",
                    min_n: int = 20, user: User = Depends(current_user),
                    session: Session = Depends(db)):
        from .. import drl as drl_mod

        try:
            return data.drl_compare(session, q, clock(), set, by, max(min_n, 1))
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e

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
        return out

    @app.get("/api/quality")
    def quality_report(q: str = "origin:local date:90d", limit: int = 200,
                       user: User = Depends(current_user), session: Session = Depends(db)):
        from .. import quality

        conds = data.conditions(data.parse(q), clock(), data.overrides(session))
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

    # ------------------------------------------------------------------ DICOM nodes

    def _nodes(fn):
        from ..net import nodes

        try:
            return fn(nodes)
        except nodes.NodeError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/dicom/status")
    def dicom_status(user: User = Depends(current_user), session: Session = Depends(db)):
        import datetime as _dt

        from ..net import nodes, qr

        nodes.import_config(session, settings)
        session.commit()
        today = clock().date()
        done = qr.done_days(session)
        days = [(today - _dt.timedelta(days=i)).isoformat() for i in range(119, -1, -1)]
        return {
            "local": {"aet": settings.dicom_aet, "port": settings.dicom_port,
                      "bind": settings.dicom_bind,
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

    # ------------------------------------------------------------------ alerts

    @app.get("/api/alerts")
    def alert_list(status: str = "open", limit: int = 50, user: User = Depends(current_user),
                   session: Session = Depends(db)):
        from .. import alerts

        return alerts.listing(session, None if status == "all" else status,
                              min(max(limit, 1), 500), names=data.device_names(session))

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
        conds = data.conditions(data.parse(q), now, data.overrides(session))
        names, physical = data.device_names(session), data._physical_ids(session)
        _audit(session, request, user, "export", f"{level}:{format}",
               {"q": q, "pid": pid})
        session.commit()
        fname = f"dosetrail-{level}s-{now:%Y%m%d-%H%M}.{format}"
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

        media = {"csv": "text/csv", "json": "application/json",
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

    @app.get("/api/drl/dataset")
    def drl_dataset(request: Request, q: str = "origin:local", format: str = "xlsx",
                    user: User = Depends(current_user), session: Session = Depends(db)):
        from ..export import drl_dataset as ds

        _exporter(user)
        if format not in ("csv", "xlsx", "json"):
            raise HTTPException(400, "format is csv|xlsx|json")
        now = clock()
        conds = data.conditions(data.parse(q), now, data.overrides(session))
        _audit(session, request, user, "export", f"drl-dataset:{format}", {"q": q})
        session.commit()

        def write(fh):
            with maker() as s:
                ds.write(format, fh, ds.dataset_rows(s, conds, secret), ds.metadata(q, now))

        return _file(write, format, "dosetrail-drl-dataset", now)

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
        conds = data.conditions(data.parse(q), now, data.overrides(session))
        try:
            rows = ds.summary_rows(session, conds, set, max(min_n, 1))
        except drl_mod.DRLError as e:
            raise HTTPException(400, str(e)) from e
        _audit(session, request, user, "export", f"drl-summary:{format}",
               {"q": q, "set": set, "min_n": min_n})
        session.commit()
        meta = {**ds.metadata(q, now, len(rows)), "dataset": "dosetrail-icrp135-summary",
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

        return _file(write, format, "dosetrail-icrp135-summary", now)

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
        conds = [*data.conditions(data.parse(q), now, data.overrides(session)),
                 Study.study_datetime >= start, Study.study_datetime < end]
        _audit(session, request, user, "export", f"survey:{month}:{format}", {"q": q})
        session.commit()
        meta = {**ds.metadata(q, now), "dataset": "dosetrail-paediatric-survey",
                "version": survey.SURVEY_VERSION, "month": month,
                "plan": "MS-CNCAN-SDNP-2026-001 draft v1.0 (March 2026)"}

        def write(fh):
            with maker() as s:
                ds.write(format, fh, survey.rows(s, conds, secret), meta, survey.DICTIONARY)

        return _file(write, format, f"dosetrail-survey-{month}", now)

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
        conds = data.conditions(data.parse(q), now, data.overrides(session))
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
        conds = data.conditions(data.parse(q), now, data.overrides(session))
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
