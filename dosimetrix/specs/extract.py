"""Rows for a spec to read: a study with its patient, device and, when needed, event and code.

``Row`` is what a field function receives. ``studies`` yields one row per study of the selection;
the exam-level extractors (study x code, event x code) are added with the kinds that use them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import select

from ..db.models import Device, Event, Patient, Study
from .context import Context

BATCH = 2000


@dataclass
class Row:
    ctx: Context
    s: Study
    p: Patient | None = None
    d: Device | None = None
    e: Event | None = None
    code: object | None = None  # db.models.Code of the exam this row is about
    events: list = field(default_factory=list)
    values: dict = field(default_factory=dict)  # per-row numbers a kind computed (sums, activity)


def window_conds(ctx: Context) -> list:
    conds = list(ctx.conds)
    if ctx.start is not None:
        conds.append(Study.study_datetime >= ctx.start)
    if ctx.end is not None:
        conds.append(Study.study_datetime < ctx.end)
    return conds


def studies(ctx: Context, extra: list | None = None) -> Iterator[Row]:
    """The studies of the selection in time order, with patient and device."""
    q = (select(Study, Patient, Device).outerjoin(Patient, Patient.id == Study.patient_id)
         .outerjoin(Device, Device.id == Study.device_id)
         .where(*window_conds(ctx), *(extra or []))
         .order_by(Study.study_datetime, Study.id))
    for s, p, d in ctx.session.execute(q):
        yield Row(ctx, s, p, d)


def parse_when(value: str | dt.datetime | None) -> dt.datetime | None:
    if isinstance(value, dt.datetime) or value is None:
        return value
    return dt.datetime.fromisoformat(value)
