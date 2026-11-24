"""Rows for a spec to read: a study with its patient, device and, when needed, event and code.

``Row`` is what a field function receives. ``studies`` yields one row per study of the selection;
the exam-level extractors (study x code, event x code) are added with the kinds that use them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy import select

from ..db.models import Classification, Code, CodeSystem, Device, Event, Patient, Study
from . import registry
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


# ----------------------------------------------------------------------------- examinations


def _first(fields: list[str], row: Row):
    for name in fields:
        v = registry.FIELDS[name].fn(row)
        if v is not None:
            return v
    return None


def exam_units(ctx: Context, system: str, values: dict, exclude: list[str] | tuple = ()) -> dict:
    """One examination = a study x a national code of ``system``.

    Returns ``{(study_id, code_id): {"age", "sex", "device", <value keys>}}``. ``values`` says how
    each number is found (a table of ``key = {event = [fields], study = field, scale = k}``):
    * a code given to **events** (radiography projections, mammography views): the value is the
      sum, over the exam's events, of the first present field of ``event``;
    * a code given to the **study**: the ``study`` field, times ``scale``.
    An exam with event-level codes is never overwritten by the study-level one.
    """
    base = (select(Classification.study_id, Classification.code_id, Classification.event_id,
                   Study, Patient, Event)
            .join(Code, Code.id == Classification.code_id)
            .join(CodeSystem, CodeSystem.id == Code.system_id)
            .join(Study, Study.id == Classification.study_id)
            .outerjoin(Patient, Patient.id == Study.patient_id)
            .outerjoin(Event, Event.id == Classification.event_id)
            .where(CodeSystem.key == system, *window_conds(ctx)))
    units: dict[tuple, dict] = {}
    study_level = []
    for sid, cid, eid, s, p, e in ctx.session.execute(base):
        if eid is None:
            study_level.append((sid, cid, s, p))
            continue
        u = units.setdefault((sid, cid), {"age": s.age_days, "sex": p.sex if p else None,
                                          "device": s.device_id, **{k: None for k in values}})
        row = Row(ctx, s, p, e=e)
        for k, v in values.items():
            if v.get("event"):
                val = _first(v["event"], row)
                if val is not None:
                    u[k] = (u[k] or 0.0) + val * v.get("scale", 1)
    for sid, cid, s, p in study_level:
        if (sid, cid) in units:
            continue
        row = Row(ctx, s, p)
        u = {"age": s.age_days, "sex": p.sex if p else None, "device": s.device_id}
        for k, v in values.items():
            val = registry.FIELDS[v["study"]].fn(row) if v.get("study") else None
            u[k] = val * v.get("scale", 1) if val is not None else None
        units[(sid, cid)] = u
    for name in exclude:
        units = registry.call(name, (ctx, units), registry.FILTERS)
    return units


@registry.filter_("shared_administration")
def _drop_shared_administration(ctx_units, *_):
    """An injection split over two studies (early and delayed PET) is one examination: the later
    study, whose every administration is counted on an earlier one, is left out."""
    ctx, units = ctx_units
    shared = shared_administration(ctx.session, {sid for sid, _ in units})
    return {k: u for k, u in units.items() if k[0] not in shared}


def shared_administration(session, study_ids: set[int]) -> set[int]:
    """Studies whose every administration is counted on an earlier study."""
    own: dict[int, bool] = {}
    ids = sorted(study_ids)
    for i in range(0, len(ids), 1000):
        for sid, extra in session.execute(
                select(Event.study_id, Event.extra_json)
                .where(Event.study_id.in_(ids[i:i + 1000]),
                       Event.event_type == "radiopharmaceutical")):
            shared = bool((extra or {}).get("same_administration_as"))
            own[sid] = own.get(sid, False) or not shared
    return {sid for sid, has_own in own.items() if not has_own}


def exam_rows(ctx: Context, system: str, modalities: list[str], values: dict,
              per_event: bool = False, exclude: list[str] | tuple = ()) -> dict:
    """The examinations of ``system`` as rows (study, patient, device, code, first event) with the
    sums of ``values`` in ``row.values``. ``per_event``: one row per event (a mammography view)
    instead of per study x code. Returns ``{key: Row}`` in time order."""
    q = (select(Classification.study_id, Classification.code_id, Classification.event_id,
                Code, Study, Patient, Device, Event)
         .join(Code, Code.id == Classification.code_id)
         .join(CodeSystem, CodeSystem.id == Code.system_id)
         .join(Study, Study.id == Classification.study_id)
         .outerjoin(Patient, Patient.id == Study.patient_id)
         .outerjoin(Device, Device.id == Study.device_id)
         .outerjoin(Event, Event.id == Classification.event_id)
         .where(CodeSystem.key == system, Study.modality.in_(modalities), *window_conds(ctx))
         .order_by(Study.study_datetime, Study.id, Code.sort_order, Classification.event_id))
    rows: dict[tuple, Row] = {}
    for sid, cid, eid, code, s, p, d, ev in ctx.session.execute(q):
        key = (sid, cid, eid) if per_event else (sid, cid)
        row = rows.get(key)
        if row is None:
            row = rows[key] = Row(ctx, s, p, d, ev, code, values={k: None for k in values})
        if ev is not None and not per_event:
            probe = Row(ctx, s, p, d, ev, code)
            for k, v in values.items():
                if v.get("event"):
                    val = _first(v["event"], probe)
                    if val is not None:
                        row.values[k] = (row.values[k] or 0.0) + val * v.get("scale", 1)
        elif ev is None:
            for k, v in values.items():
                if v.get("study"):
                    got = registry.FIELDS[v["study"]].fn(row)
                    row.values[k] = got * v.get("scale", 1) if got is not None else None
    for name in exclude:
        rows = registry.call(name, (ctx, rows), registry.FILTERS)
    return rows
