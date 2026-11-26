"""QA and calibration register per device.

The medical physicist records what was done to a device and when the next one is due:

* ``dap_calibration``: the DAP meter was checked against a reference, with a correction factor
  (true DAP = displayed × factor). The factor applies to the studies from the date of the
  calibration until the next calibration of the same device; before the first one it is 1. The
  stored DAP is never changed: the corrected value is shown beside it (study page, dose
  statement) and used by the collimation audit, so a wrong factor is corrected by editing the
  register, not the data.
* ``constancy``, ``acceptance``, ``annual``, ``other``: tests, with a result and the date the
  next is due.

``due`` lists what is overdue or due soon; the alert kind ``qa_due`` (alerts.py) raises it, once
per entry and day window.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import Device, DeviceQA

KINDS = ("dap_calibration", "constancy", "acceptance", "annual", "other")
LABELS = {"dap_calibration": "DAP meter calibration", "constancy": "constancy test",
          "acceptance": "acceptance test", "annual": "annual check", "other": "QA check"}
FACTOR_RANGE = (0.5, 2.0)  # beyond it the meter is broken or the entry is a typo


class QAError(ValueError):
    pass


def add(session: Session, device_id: int, user: str | None, *, kind: str, done_on: dt.date,
        next_due: dt.date | None = None, factor: float | None = None, result: str | None = None,
        note: str | None = None) -> DeviceQA:
    if session.get(Device, device_id) is None:
        raise QAError("device not found")
    if kind not in KINDS:
        raise QAError(f"kind is one of {', '.join(KINDS)}")
    if kind == "dap_calibration":
        if factor is None:
            raise QAError("a DAP calibration needs its correction factor")
        if not FACTOR_RANGE[0] <= factor <= FACTOR_RANGE[1]:
            raise QAError(f"the factor is {FACTOR_RANGE[0]}..{FACTOR_RANGE[1]}")
    elif factor is not None:
        raise QAError("only a DAP calibration has a factor")
    if next_due is not None and next_due <= done_on:
        raise QAError("the next due date is after the date done")
    e = DeviceQA(device_id=device_id, kind=kind, done_on=done_on, next_due=next_due,
                 factor=factor, result=(result or "").strip()[:255] or None,
                 note=(note or "").strip()[:1024] or None, entered_by=user)
    session.add(e)
    session.flush()
    return e


def remove(session: Session, entry_id: int) -> int:
    e = session.get(DeviceQA, entry_id)
    if e is None:
        raise QAError("entry not found")
    device_id = e.device_id
    session.delete(e)
    session.flush()
    return device_id


def _row(e: DeviceQA) -> dict:
    return {"id": e.id, "device_id": e.device_id, "kind": e.kind, "done_on": e.done_on.isoformat(),
            "next_due": e.next_due.isoformat() if e.next_due else None, "factor": e.factor,
            "result": e.result, "note": e.note, "entered_by": e.entered_by}


def entries(session: Session, device_id: int) -> list[dict]:
    q = select(DeviceQA).where(DeviceQA.device_id == device_id)
    return [_row(e) for e in session.scalars(q.order_by(DeviceQA.done_on.desc(),
                                                        DeviceQA.id.desc()))]


class Factors:
    """DAP correction factors of every device, by date: ``at(device_id, when)``."""

    def __init__(self, session: Session):
        self._by: dict[int, list[tuple[dt.date, float]]] = defaultdict(list)
        phys = {i: (m or i) for i, m in session.execute(select(Device.id, Device.merged_into_id))}
        for did, done, f in session.execute(
                select(DeviceQA.device_id, DeviceQA.done_on, DeviceQA.factor)
                .where(DeviceQA.kind == "dap_calibration", DeviceQA.factor.is_not(None))
                .order_by(DeviceQA.done_on)):
            self._by[phys.get(did, did)].append((done, f))
        self._phys = phys

    def at(self, device_id: int | None, when: dt.datetime | dt.date | None) -> float:
        if device_id is None or when is None:
            return 1.0
        day = when.date() if isinstance(when, dt.datetime) else when
        f = 1.0
        for done, factor in self._by.get(self._phys.get(device_id, device_id), ()):
            if done <= day:
                f = factor
            else:
                break
        return f


def factor_at(session: Session, device_id: int | None, when: dt.datetime | None) -> float:
    return Factors(session).at(device_id, when)


SOON_DAYS = 30


def due(session: Session, today: dt.date, soon_days: int = SOON_DAYS,
        names: dict[int, str] | None = None) -> list[dict]:
    """Per device and kind, the latest entry's next-due date when it is overdue or within
    ``soon_days``; overdue first."""
    latest: dict[tuple[int, str], DeviceQA] = {}
    for e in session.scalars(select(DeviceQA).order_by(DeviceQA.done_on, DeviceQA.id)):
        latest[(e.device_id, e.kind)] = e
    out = []
    for (did, kind), e in latest.items():
        if e.next_due is None:
            continue
        days = (e.next_due - today).days
        if days <= soon_days:
            out.append({"device_id": did, "device": (names or {}).get(did), "kind": kind,
                        "done_on": e.done_on.isoformat(), "next_due": e.next_due.isoformat(),
                        "days": days, "overdue": days < 0, "entry_id": e.id})
    out.sort(key=lambda r: (r["days"], r["device_id"], r["kind"]))
    return out
