"""Corrections for known errors in OpenREM's stored values.

Each fix names the equipment it applies to (manufacturer + model, as stored on the device
record), the event column, a multiplication factor and the reason. Fixes only touch events of
studies imported from OpenREM (``study.source_kind == "openrem"``); data Dosimetrix reads from
DICOM itself is parsed correctly in the first place.

Applying is idempotent: every corrected event records the fix name in
``extra_json["fixes"]``, and an event that already carries it is skipped. Study totals of the
touched studies are recomputed and every run writes one audit-log entry.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import AuditLog, Device, Event, Study
from ...ingest.store import update_study_totals


@dataclass(frozen=True)
class Fix:
    name: str
    manufacturer: str
    model: str
    column: str
    factor: float
    reason: str


KNOWN_ERRORS: tuple[Fix, ...] = (
    Fix("canon-ultimax-rp-mgy", "CANON_MEC", "Platinum43", "rp_dose", 1e-3,
        "The Canon Ultimax RDSR reports reference-point air kerma in mGy; OpenREM stored "
        "the number as Gy (1000x too high)."),
)


def _matches(fix: Fix):
    return ((func.upper(func.trim(Device.manufacturer)) == fix.manufacturer.upper())
            & (func.upper(func.trim(Device.model_name)) == fix.model.upper()))


def apply_known_fixes(session: Session, fixes: tuple[Fix, ...] = KNOWN_ERRORS,
                      username: str | None = None) -> list[str]:
    """Apply every fix that still has uncorrected events; returns report lines."""
    lines = []
    for fix in fixes:
        col = getattr(Event, fix.column)
        events = session.scalars(
            select(Event).join(Study, Study.id == Event.study_id)
            .join(Device, Device.id == Study.device_id)
            .where(Study.source_kind == "openrem", _matches(fix), col.is_not(None)))
        touched: set[int] = set()
        n = 0
        for ev in events:
            extra = dict(ev.extra_json or {})
            done = list(extra.get("fixes") or [])
            if fix.name in done:
                continue
            setattr(ev, fix.column, getattr(ev, fix.column) * fix.factor)
            extra["fixes"] = [*done, fix.name]
            ev.extra_json = extra
            touched.add(ev.study_id)
            n += 1
        session.flush()
        for study in session.scalars(select(Study).where(Study.id.in_(touched))):
            update_study_totals(session, study)
        if n:
            session.add(AuditLog(username=username, action="openrem.fix", target=fix.name,
                                 detail={"events": n, "studies": len(touched),
                                         "column": fix.column, "factor": fix.factor,
                                         "reason": fix.reason}))
            session.flush()
        lines.append(f"{fix.name}: corrected {n} event(s) in {len(touched)} study(ies)"
                     if n else f"{fix.name}: nothing to correct")
    return lines


def pending(session: Session) -> Counter:
    """Events per fix that would still be corrected (for reports and tests)."""
    out: Counter = Counter()
    for fix in KNOWN_ERRORS:
        for (extra,) in session.execute(
                select(Event.extra_json).join(Study, Study.id == Event.study_id)
                .join(Device, Device.id == Study.device_id)
                .where(Study.source_kind == "openrem", _matches(fix),
                       getattr(Event, fix.column).is_not(None))):
            if fix.name not in ((extra or {}).get("fixes") or []):
                out[fix.name] += 1
    return out


def backfill_ct_phantom(session: Session, dump_dir, username: str | None = None,
                        apply: bool = True) -> dict:
    """Fill ``event.ct_phantom_cm`` of OpenREM-imported CT series from the dump.

    Earlier imports read the phantom size from digits in OpenREM's code meaning, which has
    none ("IEC Body Dosimetry Phantom"), so the field stayed empty. Only empty values are
    filled (idempotent); events are matched by irradiation event UID."""
    from pathlib import Path

    from .importer import phantom_cm, rows

    want: dict[str, int | None] = {}
    for r in rows(Path(dump_dir) / "ct_events.csv.gz"):
        if r.get("irradiation_event_uid"):
            want[r["irradiation_event_uid"][:64]] = phantom_cm(r.get("phantom"))
    q = (select(Event).join(Study, Study.id == Event.study_id)
         .where(Study.source_kind == "openrem", Study.modality == "CT",
                Event.ct_phantom_cm.is_(None), Event.event_uid.is_not(None)))
    counts: Counter = Counter()
    for ev in session.scalars(q):
        if ev.event_uid not in want:
            counts["not_in_dump"] += 1
            continue
        cm = want[ev.event_uid]
        if cm is None:
            counts["no_phantom_in_dump"] += 1
            continue
        counts[f"{cm}cm"] += 1
        if apply:
            ev.ct_phantom_cm = cm
    if apply and (counts["16cm"] or counts["32cm"]):
        session.add(AuditLog(username=username, action="openrem.fix",
                             target="ct-phantom-backfill", detail=dict(counts)))
    session.flush()
    return dict(counts)
