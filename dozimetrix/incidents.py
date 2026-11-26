"""Investigation and incident records: an alert becomes a case with a cause, actions, outcome and
a decision on whether it is reportable as an unintended or accidental exposure to the national
regulator (BSS Directive 2013/59/Euratom art. 63). Gives the medical physicist an audit trail.

* An incident is opened from an alert (its study comes with it), or from a study, with a title.
* Every change of cause / actions / outcome / reportability and every note is written to the
  incident's log with who and when; the log is append-only.
* Closing needs an outcome and a decision on reportability (yes or no, with the basis); an
  incident decided reportable also records when and under which reference it was reported before
  it can be closed.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import Alert, AlertRule, Incident, IncidentLog, Study

FIELDS = ("title", "cause", "actions", "outcome", "reportable", "reportable_basis",
          "reported_at", "report_reference")


class IncidentError(ValueError):
    pass


def _log(session: Session, inc: Incident, user: str | None, now: dt.datetime, kind: str,
         text: str) -> None:
    inc.log.append(IncidentLog(at=now, username=user, kind=kind, text=text))


def _show(v) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return str(v)


def open_incident(session: Session, user: str, now: dt.datetime, *, alert_id: int | None = None,
                  study_id: int | None = None, title: str | None = None) -> Incident:
    """Open an incident for an alert (one open incident per alert), or for a study."""
    if alert_id is None and study_id is None:
        raise IncidentError("an alert or a study is required")
    if alert_id is not None:
        a = session.get(Alert, alert_id)
        if a is None:
            raise IncidentError("alert not found")
        dup = session.scalar(select(Incident).where(Incident.alert_id == alert_id,
                                                    Incident.status == "open"))
        if dup is not None:
            raise IncidentError(f"incident {dup.id} is already open for this alert")
        study_id = a.study_id
        title = title or f"{session.get(AlertRule, a.rule_id).name}: {a.message}"
    elif session.get(Study, study_id) is None:
        raise IncidentError("study not found")
    if not title or not title.strip():
        raise IncidentError("a title is required")
    inc = Incident(title=title.strip()[:255], alert_id=alert_id, study_id=study_id, status="open",
                   opened_by=user)
    session.add(inc)
    session.flush()
    _log(session, inc, user, now, "opened", inc.title)
    return inc


def update(session: Session, incident_id: int, user: str, now: dt.datetime,
           **changes) -> Incident:
    """Change fields; each change is logged as ``field: old → new``."""
    inc = session.get(Incident, incident_id)
    if inc is None:
        raise IncidentError("incident not found")
    if inc.status == "closed":
        raise IncidentError("the incident is closed: reopen it to change it")
    for k, v in changes.items():
        if k not in FIELDS:
            raise IncidentError(f"unknown field {k}")
        if isinstance(v, str):
            v = v.strip() or None
        if k == "title" and not v:
            raise IncidentError("a title is required")
        old = getattr(inc, k)
        if old == v:
            continue
        setattr(inc, k, v)
        _log(session, inc, user, now, "change", f"{k}: {_show(old)} → {_show(v)}")
    session.flush()
    return inc


def note(session: Session, incident_id: int, user: str, now: dt.datetime, text: str) -> None:
    inc = session.get(Incident, incident_id)
    if inc is None:
        raise IncidentError("incident not found")
    if not text or not text.strip():
        raise IncidentError("the note is empty")
    _log(session, inc, user, now, "note", text.strip())
    session.flush()


def close(session: Session, incident_id: int, user: str, now: dt.datetime) -> Incident:
    inc = session.get(Incident, incident_id)
    if inc is None:
        raise IncidentError("incident not found")
    if inc.status == "closed":
        raise IncidentError("the incident is already closed")
    if not inc.outcome:
        raise IncidentError("record the outcome before closing")
    if inc.reportable is None:
        raise IncidentError("decide whether it is reportable (yes or no) before closing")
    if inc.reportable and not (inc.reported_at and inc.report_reference):
        raise IncidentError("a reportable incident needs the date and reference of the report")
    inc.status, inc.closed_at = "closed", now
    _log(session, inc, user, now, "closed", inc.outcome)
    session.flush()
    return inc


def reopen(session: Session, incident_id: int, user: str, now: dt.datetime,
           reason: str) -> Incident:
    inc = session.get(Incident, incident_id)
    if inc is None:
        raise IncidentError("incident not found")
    if inc.status != "closed":
        raise IncidentError("the incident is not closed")
    if not reason or not reason.strip():
        raise IncidentError("say why it is reopened")
    inc.status, inc.closed_at = "open", None
    _log(session, inc, user, now, "reopened", reason.strip())
    session.flush()
    return inc


def _summary(i: Incident) -> dict:
    return {"id": i.id, "title": i.title, "status": i.status, "alert_id": i.alert_id,
            "study_id": i.study_id, "opened_by": i.opened_by,
            "opened_at": i.created_at.isoformat(timespec="minutes") if i.created_at else None,
            "closed_at": i.closed_at.isoformat(timespec="minutes") if i.closed_at else None,
            "reportable": i.reportable,
            "reported": bool(i.reported_at)}


def listing(session: Session, status: str | None = None, limit: int = 200) -> dict:
    q = select(Incident)
    if status:
        if status not in ("open", "closed"):
            raise IncidentError("status is open or closed")
        q = q.where(Incident.status == status)
    rows = [_summary(i) for i in session.scalars(q.order_by(Incident.id.desc()).limit(limit))]
    counts = dict(session.execute(select(Incident.status, func.count())
                                  .group_by(Incident.status)).all())
    unassessed = session.scalar(select(func.count()).where(Incident.reportable.is_(None),
                                                          Incident.status == "open"))
    return {"counts": counts, "unassessed": unassessed, "rows": rows}


def detail(session: Session, incident_id: int) -> dict | None:
    i = session.get(Incident, incident_id)
    if i is None:
        return None
    return {**_summary(i), "cause": i.cause, "actions": i.actions, "outcome": i.outcome,
            "reportable_basis": i.reportable_basis, "report_reference": i.report_reference,
            "reported_at": i.reported_at.isoformat(timespec="minutes") if i.reported_at
            else None,
            "log": [{"at": e.at.isoformat(timespec="seconds"), "user": e.username,
                     "kind": e.kind, "text": e.text} for e in i.log]}
