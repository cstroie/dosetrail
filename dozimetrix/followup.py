"""High-dose follow-up for fluoroscopy and interventional work.

A study that passes a substantial radiation dose level (SIR / NCRP Report 168) is followed up:
the patient is told, the skin is checked 2-4 weeks later (deterministic skin effects show after
days to weeks), and the outcome is recorded. The levels, from the stored totals:

* peak skin dose ≥ 3 Gy (``Study.psd_mgy``, estimated);
* reference-point air kerma Ka,r ≥ 5 Gy;
* air kerma-area product ≥ 500 Gy·cm².

``sync`` creates one ``Followup`` per study that passes any of them (a study already followed is
kept as it is, with its status); the alert rules of the same levels say *something happened*, this
is the *what was done about it* record an inspection asks for. ``letter`` writes the patient letter
(Romanian or English), which names the patient: it is for users with the pid role.
"""

from __future__ import annotations

import datetime as dt
import html
from collections import Counter
from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import Device, Followup, Patient, Study

MODALITIES = ("RF", "XA")
PSD_MGY, KAR_GY, KAP_GYCM2 = 3000.0, 5.0, 500.0
CHECK_AFTER_DAYS, CHECK_UNTIL_DAYS = 14, 28
STATUSES = ("open", "informed", "checked", "dismissed")
BATCH = 1000


def levels(s: Study) -> str | None:
    """The levels a study passes, as text with the values; None when it passes none."""
    hit = []
    if s.psd_mgy is not None and s.psd_mgy >= PSD_MGY:
        hit.append(f"peak skin dose {s.psd_mgy:.0f} mGy ≥ {PSD_MGY:.0f}")
    if s.total_rp_dose is not None and s.total_rp_dose >= KAR_GY:
        hit.append(f"Ka,r {s.total_rp_dose * 1000:.0f} mGy ≥ {KAR_GY * 1000:.0f}")
    if s.total_dap is not None and s.total_dap >= KAP_GYCM2:
        hit.append(f"KAP {s.total_dap * 1000:.0f} mGy·cm² ≥ {KAP_GYCM2 * 1000:.0f}")
    return "; ".join(hit) or None


def sync(session: Session, study_ids: Iterable[int] | None = None) -> Counter:
    """Create the follow-up of each fluoroscopy / interventional study that passes a level (all
    studies when ``study_ids`` is None). Idempotent: an existing record is only refreshed
    (the levels text) while it is open, so a changed estimate is not lost."""
    stats: Counter = Counter()
    ids = None if study_ids is None else sorted(set(study_ids))
    chunks = [None] if ids is None else [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    for chunk in chunks:
        q = select(Study).where(Study.modality.in_(MODALITIES))
        if chunk is not None:
            q = q.where(Study.id.in_(chunk))
        else:
            q = q.where(or_(Study.psd_mgy >= PSD_MGY, Study.total_rp_dose >= KAR_GY,
                            Study.total_dap >= KAP_GYCM2))
        studies = list(session.scalars(q))
        existing = {f.study_id: f for f in session.scalars(select(Followup).where(
            Followup.study_id.in_([s.id for s in studies])))}
        for s in studies:
            text = levels(s)
            old = existing.get(s.id)
            if old is not None:
                if text and old.status == "open" and old.levels != text[:255]:
                    old.levels = text[:255]
                    stats["updated"] += 1
                continue
            if not text:
                continue
            due = (s.study_datetime + dt.timedelta(days=CHECK_AFTER_DAYS)).date() \
                if s.study_datetime else None
            try:
                with session.begin_nested():
                    session.add(Followup(study_id=s.id, study_datetime=s.study_datetime,
                                         levels=text[:255], status="open", check_due=due))
            except IntegrityError:  # a concurrent worker created it first
                continue
            stats["new"] += 1
    session.flush()
    return stats


def _row(f: Followup, s: Study, device: str | None, today: dt.date) -> dict:
    overdue = (f.status in ("open", "informed") and s.study_datetime is not None
               and today > (s.study_datetime + dt.timedelta(days=CHECK_UNTIL_DAYS)).date())
    return {"id": f.id, "study_id": f.study_id, "modality": s.modality, "device": device,
            "time": s.study_datetime.isoformat(timespec="minutes") if s.study_datetime else None,
            "exam": s.procedure_name or s.study_description, "levels": f.levels,
            "status": f.status, "overdue": overdue,
            "informed_at": f.informed_at.isoformat(timespec="minutes") if f.informed_at else None,
            "informed_by": f.informed_by,
            "check_due": f.check_due.isoformat() if f.check_due else None,
            "checked_at": f.checked_at.isoformat(timespec="minutes") if f.checked_at else None,
            "outcome": f.outcome, "note": f.note, "updated_by": f.updated_by}


def listing(session: Session, status: str | None = None, today: dt.date | None = None,
            limit: int = 200, names: dict[int, str] | None = None) -> dict:
    """Follow-ups, the ones to act on first: overdue, then by date. ``status`` ``pending``
    means open or informed."""
    today = today or dt.date.today()
    q = select(Followup, Study).join(Study, Study.id == Followup.study_id)
    if status == "pending":
        q = q.where(Followup.status.in_(("open", "informed")))
    elif status:
        q = q.where(Followup.status == status)
    rows = [_row(f, s, (names or {}).get(s.device_id), today)
            for f, s in session.execute(q.order_by(Followup.study_datetime.desc(),
                                                   Followup.id.desc()))]
    rows.sort(key=lambda r: (not r["overdue"],))  # stable: overdue first, newest first within
    counts = Counter(r["status"] for r in rows)
    return {"counts": dict(counts), "overdue": sum(r["overdue"] for r in rows),
            "rows": rows[:limit]}


class FollowupError(ValueError):
    pass


def update(session: Session, followup_id: int, user: str, now: dt.datetime, *,
           status: str | None = None, outcome: str | None = None, note: str | None = None,
           check_due: dt.date | None = None) -> Followup:
    """Set the status (the date and user of telling the patient or of the check are recorded
    with it), outcome, note or the check date."""
    f = session.get(Followup, followup_id)
    if f is None:
        raise FollowupError("follow-up not found")
    if status is not None:
        if status not in STATUSES:
            raise FollowupError(f"status is one of {', '.join(STATUSES)}")
        if status == "informed" and f.informed_at is None:
            f.informed_at, f.informed_by = now, user
        if status == "checked":
            if not outcome and not f.outcome:
                raise FollowupError("record the outcome of the skin check")
            f.checked_at = f.checked_at or now
            if f.informed_at is None:
                f.informed_at, f.informed_by = now, user
        if status == "dismissed" and not (note or f.note):
            raise FollowupError("say why no follow-up is needed")
        f.status = status
    if outcome is not None:
        f.outcome = outcome.strip()[:255] or None
    if note is not None:
        f.note = note.strip()[:1024] or None
    if check_due is not None:
        f.check_due = check_due
    f.updated_by = user
    session.flush()
    return f


LETTERS = {
    "ro": {
        "title": "Informare privind doza de radiații",
        "greet": "Stimate pacient,",
        "body": [
            "La data de {when} ați efectuat procedura „{exam}” cu radioscopie / angiografie, la "
            "{institution}. Procedura a necesitat o doză de radiații mai mare decât cea uzuală "
            "({levels}).",
            "La acest nivel de doză, pielea din zona expusă poate prezenta, după câteva zile până "
            "la câteva săptămâni, înroșire, uscăciune, descuamare sau, rar, leziuni mai "
            "profunde. Nu este un motiv de îngrijorare imediată, dar vă rugăm să urmăriți "
            "zona respectivă (de obicei pe spate sau pe lateralul toracelui).",
            "Dacă observați modificări ale pielii, vă rugăm să vă prezentați la medic. Oricum, vă "
            "rugăm să vă prezentați la control cu aproximativ {check} (între 2 și 4 săptămâni de "
            "la procedură). Evitați expunerea zonei la soare și frecarea ei, iar la orice altă "
            "procedură cu radiații anunțați că ați primit această doză.",
        ],
        "close": "Cu stimă,", "patient": "Pacient", "date": "Data", "sign": "Semnătura medicului",
    },
    "en": {
        "title": "Information about your radiation dose",
        "greet": "Dear patient,",
        "body": [
            "On {when} you had the procedure “{exam}” using fluoroscopy / angiography at "
            "{institution}. It needed a higher radiation dose than usual ({levels}).",
            "At this dose the skin in the exposed area may show, from a few days to a few weeks "
            "later, redness, dryness, peeling or, rarely, deeper damage. It is not a cause for "
            "immediate worry, but please watch the area (usually on the back or the side of the "
            "chest).",
            "If you notice any skin change, please see a doctor. In any case please come for a "
            "check around {check} (2 to 4 weeks after the procedure). Keep the area out of the "
            "sun and avoid rubbing it, and tell staff about this dose before any other "
            "procedure that uses radiation.",
        ],
        "close": "Yours sincerely,", "patient": "Patient", "date": "Date",
        "sign": "Physician's signature",
    },
}


def letter(session: Session, followup_id: int, lang: str = "ro") -> str | None:
    """The printable patient letter as an HTML page (names the patient: pid role only)."""
    if lang not in LETTERS:
        raise FollowupError("lang is ro or en")
    row = session.execute(
        select(Followup, Study, Patient, Device).join(Study, Study.id == Followup.study_id)
        .outerjoin(Patient, Patient.id == Study.patient_id)
        .outerjoin(Device, Device.id == Study.device_id).where(Followup.id == followup_id)
    ).first()
    if row is None:
        return None
    f, s, p, dev = row
    t = LETTERS[lang]
    esc = html.escape
    check = f.check_due.strftime("%d.%m.%Y") if f.check_due else "—"
    when = s.study_datetime.strftime("%d.%m.%Y") if s.study_datetime else "—"
    fill = {"when": when, "exam": esc(s.procedure_name or s.study_description or "—"),
            "institution": esc((dev.institution_name if dev else None) or "—"),
            "levels": esc(f.levels), "check": check}
    who = esc((p.name or "").replace("^", " ").strip()) if p and p.name else "—"
    paras = "".join(f"<p>{para.format(**fill)}</p>" for para in t["body"])
    return (f"<!doctype html><html lang=\"{lang}\"><meta charset=\"utf-8\">"
            f"<title>{t['title']}</title><style>body{{font:14px/1.5 sans-serif;max-width:40em;"
            f"margin:2em auto}}.sign{{margin-top:4em}}@media print{{body{{margin:0}}}}</style>"
            f"<h1>{t['title']}</h1><p>{t['patient']}: <b>{who}</b> · {t['date']}: {when}</p>"
            f"<p>{t['greet']}</p>{paras}<p>{t['close']}</p>"
            f"<p class=\"sign\">{t['sign']}: ______________________</p></html>")
