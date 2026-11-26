"""Scheduled report digests: a monthly summary mailed to a department head.

A digest is a name, recipients and a study query (the department: ``origin:local modality:CT``, a
room, a workspace's query). On the first morning of each month the one for the month before is
built from what the pages show and mailed: the studies, the exams whose median is above the
national DRL (worst first), the open alerts of the month, and the rooms that went quiet. It names
no patient. The schedules are kept in ``app_setting`` (key ``report.digests``) and edited through
``/api/digests`` (admin); the send uses the e-mail (SMTP) and ntfy settings of the alert
notifications, so there is nothing more to configure.

The mail is HTML with a plain-text part, printable from the mail program; a PDF is not made (no PDF
dependency). A month is sent once: ``last_period`` is written after the send succeeds, so a failed
run is tried again the next hour.
"""

from __future__ import annotations

import copy
import datetime as dt
import html
import logging
import re
from collections.abc import Callable
from email.message import EmailMessage

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import AppSetting, Device, Study

log = logging.getLogger(__name__)
KEY = "report.digests"
SEND_HOUR = 7  # not before 07:00 on the 1st
TOP = 8
SILENT_DAYS = 4
SET_KEY = "ro-ms838-2025"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DigestError(ValueError):
    pass


def load(session: Session) -> list[dict]:
    row = session.get(AppSetting, KEY)
    return copy.deepcopy(list(row.value or [])) if row else []


def save(session: Session, items: list[dict]) -> list[dict]:
    """Validate and store the schedules (replacing the list); ``last_period`` is kept for the
    ones that already exist."""
    old = {d["id"]: d for d in load(session)}
    out, seen = [], set()
    for i, raw in enumerate(items):
        name = str(raw.get("name") or "").strip()
        to = [str(x).strip() for x in raw.get("to") or [] if str(x).strip()]
        if not name:
            raise DigestError(f"digest {i + 1}: a name is required")
        if not to or any(not _EMAIL.match(x) for x in to):
            raise DigestError(f"digest {name!r}: recipients must be e-mail addresses")
        did = (str(raw.get("id") or "").strip()
               or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"))
        if not did or did in seen:
            raise DigestError(f"digest {name!r}: the id {did!r} is empty or repeated")
        seen.add(did)
        out.append({"id": did[:40], "name": name[:80], "to": to[:20], "q": str(raw.get("q") or ""),
                    "enabled": bool(raw.get("enabled", True)),
                    "ntfy": bool(raw.get("ntfy", False)),
                    "last_period": old.get(did, {}).get("last_period")})
    row = session.get(AppSetting, KEY)
    if row is None:
        session.add(AppSetting(key=KEY, value=out))
    else:
        row.value = out
    session.flush()
    return out


def previous_month(now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (end - dt.timedelta(days=1)).replace(day=1)
    return start, end


def build(session: Session, q: str, start: dt.datetime, end: dt.datetime, now: dt.datetime
          ) -> dict:
    from . import alerts, drl
    from .api.data import checked_conditions, device_names, overrides

    conds = [*checked_conditions(q, now, overrides(session)),
             Study.study_datetime >= start, Study.study_datetime < end]
    n_studies = session.scalar(select(func.count()).select_from(Study).where(*conds)) or 0
    names = device_names(session)
    above = []
    try:
        res = drl.compare(session, conds, SET_KEY, by_device=True, min_n=20, names=names)
    except drl.DRLError:
        res = {"rows": []}
    for r in res["rows"]:
        for g in r.get("groups", ()):
            if g["status"] in ("above", "well_above") and g.get("ratio"):
                above.append({"exam": r["exam"], "band": r["band_label"], "room": g["device_name"],
                              "quantity": r["quantity"], "unit": r["unit"], "n": g["n"],
                              "median": g["median"], "reference": r["reference"],
                              "ratio": g["ratio"]})
    above.sort(key=lambda x: -x["ratio"])
    in_month = {"since": start, "until": end}
    ac = alerts.counts(session, None, **in_month)
    opened = alerts.counts(session, "open", **in_month)
    top_alerts = [{"time": a["time"], "title": a["title"], "message": a["message"],
                   "level": a["level"]}
                  for a in alerts.listing(session, "open", 6, names=names, **in_month)]
    quiet = []
    for d in session.scalars(select(Device).where(Device.is_local.is_(True),
                                                  Device.merged_into_id.is_(None))):
        if d.last_study_at and d.last_study_at >= now - dt.timedelta(days=90) \
                and (now - d.last_study_at).days >= SILENT_DAYS:
            quiet.append({"room": names.get(d.id) or d.display_name,
                          "last": d.last_study_at.isoformat(timespec="minutes"),
                          "days": (now - d.last_study_at).days})
    quiet.sort(key=lambda x: -x["days"])
    return {"month": start.strftime("%Y-%m"), "studies": n_studies,
            "above_total": len(above), "above": above[:TOP],
            "alerts_total": ac["total"], "alerts_open": opened["total"],
            "alerts_by_rule": ac["by_rule"][:5], "top_alerts": top_alerts, "quiet": quiet}


def _fmt(v: float | None) -> str:
    return "—" if v is None else (f"{v:.0f}" if abs(v) >= 100 else f"{v:.3g}")


def render(name: str, d: dict, base_url: str | None = None) -> tuple[str, str, str]:
    """(subject, plain text, HTML) of a digest."""
    subject = f"[Dozimetrix] {name}: {d['month']}"
    lines = [f"{name} — {d['month']}", "", f"Studies: {d['studies']}", ""]
    lines.append(f"Exams above the national DRL: {d['above_total']}")
    for r in d["above"]:
        lines.append(f"  {r['exam']} {r['band'] or ''} · {r['room'] or 'all rooms'}: median "
                     f"{_fmt(r['median'])} {r['unit']} = {r['ratio']:.2f} × DRL "
                     f"{_fmt(r['reference'])} (n = {r['n']})")
    lines += ["", f"Alerts in the month: {d['alerts_total']} ({d['alerts_open']} still open)"]
    lines += [f"  {x['name']}: {x['n']}" for x in d["alerts_by_rule"]]
    lines += [f"  open: {a['title']} — {a['message']}" for a in d["top_alerts"]]
    lines += ["", "Rooms with no study for more than 4 days:" if d["quiet"]
              else "No room has gone quiet."]
    lines += [f"  {r['room']}: last {r['last']} ({r['days']} days)" for r in d["quiet"]]
    if base_url:
        lines += ["", base_url.rstrip("/") + "/alerts.html"]
    e = html.escape
    li = lambda items: "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>" if items else ""  # noqa: E731
    body = (f"<h2>{e(name)} — {e(d['month'])}</h2><p>Studies: <b>{d['studies']}</b></p>"
            f"<h3>Exams above the national DRL: {d['above_total']}</h3>"
            + li(f"{e(r['exam'])} {e(r['band'] or '')} · {e(r['room'] or 'all rooms')}: median "
                 f"{_fmt(r['median'])} {e(r['unit'])} = <b>{r['ratio']:.2f} × DRL</b> "
                 f"{_fmt(r['reference'])} (n = {r['n']})" for r in d["above"])
            + f"<h3>Alerts in the month: {d['alerts_total']} ({d['alerts_open']} still open)</h3>"
            + li([f"{e(x['name'])}: {x['n']}" for x in d["alerts_by_rule"]]
                 + [f"<b>open</b>: {e(a['title'])} — {e(a['message'])}" for a in d["top_alerts"]])
            + "<h3>Rooms gone quiet</h3>"
            + (li(f"{e(r['room'])}: last {e(r['last'])} ({r['days']} days)" for r in d["quiet"])
               or "<p>None.</p>")
            + "<p><small>No patient data. Medians are per room, exam and age group, "
              "as on the DRL page.</small></p>")
    return subject, "\n".join(lines), f"<!doctype html><meta charset=\"utf-8\">{body}"


def _send_mail(cfg, to: list[str], subject: str, text: str, page: str) -> None:
    import smtplib
    import ssl

    from .notify import _secret

    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, ", ".join(to)
    msg["From"] = cfg.email_from or (cfg.smtp_user or "dozimetrix")
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    ctx = ssl.create_default_context()
    smtp_cls = smtplib.SMTP_SSL if cfg.smtp_ssl else smtplib.SMTP
    kwargs = {"context": ctx} if cfg.smtp_ssl else {}
    with smtp_cls(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout, **kwargs) as smtp:
        if cfg.smtp_starttls and not cfg.smtp_ssl:
            smtp.starttls(context=ctx)
        password = cfg.smtp_password or _secret(cfg.smtp_password_file)
        if cfg.smtp_user and password:
            smtp.login(cfg.smtp_user, password)
        smtp.send_message(msg)


def run_due(session: Session, cfg, now: dt.datetime | None = None, force: bool = False,
            mailer: Callable | None = None, only: str | None = None) -> dict:
    """Send the digests whose month has not gone out yet (from 07:00 on the 1st, or any time
    with ``force``). Returns {id: "sent" | "skipped" | "failed: …"}."""
    from .notify import SENDERS, Message

    now = now or dt.datetime.now()
    start, end = previous_month(now)
    period = start.strftime("%Y-%m")
    due = force or now.hour >= SEND_HOUR
    items = load(session)
    result: dict[str, str] = {}
    for d in items:
        if not d.get("enabled") or (only and d["id"] != only):
            continue
        if d.get("last_period") == period and not force:
            result[d["id"]] = "skipped"
            continue
        if not due and not force:
            continue
        if not cfg.smtp_host:
            result[d["id"]] = "failed: no e-mail server is configured"
            continue
        try:
            rep = build(session, d["q"], start, end, now)
            subject, text, page = render(d["name"], rep, cfg.base_url)
            (mailer or _send_mail)(cfg, d["to"], subject, text, page)
            if d.get("ntfy") and cfg.ntfy_url:
                SENDERS["ntfy"](cfg, Message(subject, text[:1500], cfg.base_url, False))
        except Exception as exc:  # noqa: BLE001 – retried at the next run
            log.warning("digest %s: %s", d["id"], exc)
            result[d["id"]] = f"failed: {exc}"
            continue
        d["last_period"] = period
        result[d["id"]] = "sent"
    row = session.get(AppSetting, KEY)
    if row is not None:
        row.value = list(items)  # a new list: the JSON column notices the change
    session.flush()
    return result
