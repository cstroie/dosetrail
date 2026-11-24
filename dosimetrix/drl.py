"""Compare local doses with a reference set (national DRLs, European DRLs).

For every reference value (exam code × patient band × quantity) the local values are
collected from the studies the query selects, and summarised:
* by **facility**, or by **device** (ICRP 135: the room median is the comparison unit);
* as n, median, P25/P75, mean, and the ratio median / reference value.

Studies with a DRL exclusion reason (``study.drl_exclusion``: e.g. weight below P5 or
above P95 for age, prematurity) are left out unless ``include_excluded``.

Status of a group:
* ``insufficient``: fewer than ``min_n`` values (ICRP 135 suggests about 20; paediatric
  groups often have fewer, so the value is reported but marked);
* ``below``: median ≤ reference value;
* ``above``: up to 1.5 × the reference value;
* ``well_above``: more than 1.5 × the reference value.

Local quantities by ``applies_to``:
* ``series``: one value per CT series (localisers excluded);
* ``exposure``: one value per irradiation event;
* ``study``: the sum over the study's events that carry the exam code. For radiography
  that is the dose of one projection including retakes; for fluoroscopy, the study total.
  Two exceptions:
  * mammography 2D (MG-UNILATERAL): the mean glandular dose of each breast, i.e. the sum
    over that breast's views (a bilateral study gives two values);
  * administered activity (NM, PET): the study's activity, an administration shared with
    an earlier study counted only there.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session, aliased

from .db.models import (
    Classification,
    Code,
    Device,
    Event,
    GroupBand,
    GroupScheme,
    ReferenceSet,
    ReferenceValue,
    SizeSource,
    Study,
)
from .modality_profiles import group_of
from .stats import EXPOSURE_EXAM, BandLookup, describe, own_codes

# quantity -> (event column, local unit, factor to the reference unit by unit)
QUANTITY = {
    "dlp": (Event.dlp, {"mGy.cm": 1.0}),
    "ctdivol": (Event.ctdivol, {"mGy": 1.0}),
    "dap": (Event.dap, {"Gy.cm2": 1.0, "mGy.cm2": 1000.0, "cGy.cm2": 100.0, "Gy.m2": 1e-4}),
    "mgd": (Event.agd, {"mGy": 1.0}),
    "activity": (Study.admin_activity_mbq, {"MBq": 1.0}),
}
UNAVAILABLE = {"kae": "entrance air kerma is not collected yet"}
PER_BREAST = {"MG-UNILATERAL"}  # one value per breast, not per study


class DRLError(ValueError):
    pass


def reference_sets(session: Session) -> list[dict]:
    out = []
    for rs in session.scalars(select(ReferenceSet).order_by(ReferenceSet.key)):
        out.append({"key": rs.key, "name": rs.name, "source": rs.source, "notes": rs.notes,
                    "valid_from": rs.valid_from.isoformat() if rs.valid_from else None,
                    "values": len(rs.values)})
    return out


def reference_values(session: Session, set_key: str) -> dict:
    """One set with its values, in code and band order (seeded sets are read-only)."""
    rs = session.scalar(select(ReferenceSet).filter_by(key=set_key))
    if rs is None:
        raise DRLError(f"unknown reference set {set_key!r}")
    rows = session.execute(
        select(ReferenceValue, Code, GroupBand, GroupScheme)
        .join(Code, Code.id == ReferenceValue.exam_code_id)
        .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
        .outerjoin(GroupScheme, GroupScheme.id == GroupBand.scheme_id)
        .where(ReferenceValue.set_id == rs.id)
        .order_by(Code.sort_order, Code.code, GroupScheme.key, GroupBand.sort_order,
                  ReferenceValue.quantity)).all()
    return {"key": rs.key, "name": rs.name, "source": rs.source, "notes": rs.notes,
            "valid_from": rs.valid_from.isoformat() if rs.valid_from else None,
            "values": [{"exam": c.code, "exam_meaning": c.meaning, "modality": c.modality,
                        "scheme": sc.key if sc else None, "band": b.key if b else None,
                        "band_label": b.label if b else None, "quantity": v.quantity,
                        "value": v.value, "unit": v.unit, "applies_to": v.applies_to}
                       for v, c, b, sc in rows]}


def compare(session: Session, conds: list, set_key: str, *, by_device: bool = False,
            min_n: int = 20, physical: dict[int, int] | None = None,
            names: dict[int, str] | None = None, include_excluded: bool = False,
            estimated_weights: bool = False) -> dict:
    """``estimated_weights``: let weights estimated from the growth charts place a patient
    in a weight band (off by default: a weight-band DRL needs reported weights)."""
    rs = session.scalar(select(ReferenceSet).filter_by(key=set_key))
    if rs is None:
        raise DRLError(f"unknown reference set {set_key!r}")
    refs = session.execute(
        select(ReferenceValue, Code, GroupBand, GroupScheme)
        .join(Code, Code.id == ReferenceValue.exam_code_id)
        .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
        .outerjoin(GroupScheme, GroupScheme.id == GroupBand.scheme_id)
        .where(ReferenceValue.set_id == rs.id)).all()
    physical = physical or {i: (m or i) for i, m in
                            session.execute(select(Device.id, Device.merged_into_id))}
    names = names or {d.id: d.display_name or d.station_name or f"Device {d.id}"
                      for d in session.scalars(select(Device))}

    items = [Item(key=i, exam_code_id=rv.exam_code_id, quantity=rv.quantity, unit=rv.unit,
                  applies_to=rv.applies_to, scheme=scheme.key if scheme else None,
                  parameter=scheme.parameter if scheme else None,
                  band=band.key if band else None)
             for i, (rv, _code, band, scheme) in enumerate(refs)]
    collected = collect(session, conds, items, by_device=by_device, physical=physical,
                        include_excluded=include_excluded,
                        estimated_weights=estimated_weights)
    bands: dict[str, BandLookup] = {}
    results = []
    for i, (rv, code, band, scheme) in enumerate(refs):
        item = {"exam": code.code, "exam_meaning": code.meaning,
                "group": _group(code.modality, rv.quantity),
                "band": band.key if band else None, "band_label": band.label if band else None,
                "scheme": scheme.key if scheme else None, "quantity": rv.quantity,
                "applies_to": rv.applies_to, "reference": rv.value, "unit": rv.unit}
        if scheme is not None and scheme.key not in bands:
            bands[scheme.key] = BandLookup(session, scheme.key, scheme.parameter)
        if rv.quantity not in QUANTITY:
            results.append({**item, "status": "unavailable",
                            "reason": UNAVAILABLE.get(rv.quantity, "not supported"),
                            "groups": []})
            continue
        if QUANTITY[rv.quantity][1].get(rv.unit) is None:
            results.append({**item, "status": "unavailable",
                            "reason": f"unit {rv.unit} not supported", "groups": []})
            continue
        groups = []
        for dev, vals in collected.get(i, {}).items():
            d = describe(vals)
            groups.append({"device": dev, "device_name": names.get(dev) if dev else None,
                           **d, "ratio": d["median"] / rv.value if rv.value else None,
                           "status": _status(d, rv.value, min_n)})
        groups.sort(key=lambda g: (g["device_name"] or "", -g["n"]))
        results.append({**item, "groups": groups,
                        "status": _worst([g["status"] for g in groups]) if groups else "no_data"})
    results.sort(key=lambda r: (r["exam"], r["quantity"], _band_order(r, bands)))
    return {"set": rs.key, "name": rs.name, "source": rs.source, "notes": rs.notes,
            "by": "device" if by_device else "facility", "min_n": min_n,
            "estimated_weights": estimated_weights, "rows": results}


# the modality group a reference value belongs to, when its exam code names no modality
_QUANTITY_GROUP = {"dlp": "CT", "ctdivol": "CT", "mgd": "MG", "activity": "NM"}


def _group(modality: str | None, quantity: str) -> str:
    return group_of(modality) or _QUANTITY_GROUP.get(quantity, "DX")


@dataclass(frozen=True)
class Item:
    """What to collect: an exam code, optionally a patient band, a quantity in a unit."""
    key: object
    exam_code_id: int
    quantity: str
    unit: str
    applies_to: str  # series / exposure / study
    scheme: str | None = None
    parameter: str | None = None  # age / weight
    band: str | None = None


def collect(session: Session, conds: list, items: list[Item], *, by_device: bool = False,
            physical: dict[int, int] | None = None,
            include_excluded: bool = False, estimated_weights: bool = True
            ) -> dict[object, dict[int | None, list[float]]]:
    """Local values per item and device (None = facility), in the item's unit.

    One query per quantity; every row is bucketed once by (exam code, scheme band), so many
    items cost about as much as one. ``study`` items sum the study's events of the exam;
    ``series`` / ``exposure`` items keep one value per event."""
    physical = physical or {i: (m or i) for i, m in
                            session.execute(select(Device.id, Device.merged_into_id))}
    lookups: dict[str, BandLookup] = {}
    for it in items:
        if it.scheme and it.scheme not in lookups:
            lookups[it.scheme] = BandLookup(session, it.scheme, it.parameter)
    out: dict[object, dict[int | None, list[float]]] = {}
    by_q: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        if it.quantity in QUANTITY and QUANTITY[it.quantity][1].get(it.unit) is not None:
            by_q[it.quantity].append(it)
    per_breast = set(session.scalars(select(Code.id).where(Code.code.in_(PER_BREAST))))
    for quantity, its in by_q.items():
        col, units_map = QUANTITY[quantity]
        code_ids = {it.exam_code_id for it in its}
        schemes = {it.scheme for it in its if it.scheme}
        c = aliased(Classification)
        weight = Study.weight_kg if estimated_weights else case(
            (Study.weight_source == SizeSource.estimated_reference, None),
            else_=Study.weight_kg)
        where = [col.is_not(None), *conds,
                 *([] if include_excluded else [Study.drl_exclusion.is_(None)])]
        if col.table is Study.__table__:  # one value per study (administered activity)
            q = (select(col, Study.id, Study.id, Study.device_id, Study.age_days, weight,
                        c.code_id, None)
                 .select_from(Study)
                 .join(c, and_(c.study_id == Study.id, c.code_id.in_(code_ids),
                               c.event_id.is_(None)))
                 .where(*where))
        else:
            link = and_(c.study_id == Study.id, c.code_id.in_(code_ids),
                        or_(c.event_id == Event.id,
                            and_(c.event_id.is_(None), Study.modality.not_in(EXPOSURE_EXAM),
                                 ~own_codes(Event.id))))
            q = (select(col, Event.id, Study.id, Study.device_id, Study.age_days, weight,
                        c.code_id, Event.laterality)
                 .select_from(Event).join(Study, Study.id == Event.study_id).join(c, link)
                 .where(Event.is_localizer.is_not(True), *where))
        # (code, scheme|None, band|None, per-study?) -> (unit id, device) -> sum
        buckets: dict[tuple, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        wanted = {(it.exam_code_id, it.scheme, it.band, it.applies_to == "study")
                  for it in its}
        modes = {it.applies_to == "study" for it in its}
        for value, eid, sid, did, age, weight, cid, side in session.execute(q):
            unit = (sid, side) if cid in per_breast else sid
            dev = (physical.get(did, did) if did is not None else None) if by_device else None
            keys = [(cid, None, None)]
            for sk in schemes:
                lk = lookups[sk]
                keys.append((cid, sk, lk(age if lk.parameter == "age" else weight)))
            for k in keys:
                for per_study in modes:
                    if (*k, per_study) in wanted:
                        buckets[(*k, per_study)][(unit if per_study else eid, dev)] += value
        for it in its:
            factor = units_map[it.unit]
            per_dev: dict[int | None, list[float]] = defaultdict(list)
            for (_uid, dev), v in buckets.get((it.exam_code_id, it.scheme, it.band,
                                               it.applies_to == "study"), {}).items():
                per_dev[dev].append(v * factor)
            out[it.key] = dict(per_dev)
    return out


def _status(d: dict, ref: float, min_n: int) -> str:
    if d["n"] < min_n:
        return "insufficient"
    if d["median"] <= ref:
        return "below"
    return "above" if d["median"] <= 1.5 * ref else "well_above"


_RANK = {"well_above": 4, "above": 3, "below": 2, "insufficient": 1, "no_data": 0}


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 0))


def _band_order(r: dict, bands: dict[str, BandLookup]) -> int:
    lk = bands.get(r["scheme"]) if r["scheme"] else None
    return lk.order.get(r["band"], 99) if lk else -1
