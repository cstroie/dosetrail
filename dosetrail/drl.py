"""Compare local doses with a reference set (national DRLs, European DRLs).

For every reference value (exam code × patient band × quantity) the local values are
collected from the studies the query selects, and summarised:
* by **facility**, or by **device** (ICRP 135: the room median is the comparison unit);
* as n, median, P25/P75, mean, and the ratio median / reference value.

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
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import and_, or_, select
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
    Study,
)
from .stats import EXPOSURE_EXAM, BandLookup, describe, own_codes

# quantity -> (event column, local unit, factor to the reference unit by unit)
QUANTITY = {
    "dlp": (Event.dlp, {"mGy.cm": 1.0}),
    "ctdivol": (Event.ctdivol, {"mGy": 1.0}),
    "dap": (Event.dap, {"Gy.cm2": 1.0, "mGy.cm2": 1000.0, "cGy.cm2": 100.0, "Gy.m2": 1e-4}),
    "mgd": (Event.agd, {"mGy": 1.0}),
}
UNAVAILABLE = {"kae": "entrance air kerma is not collected yet",
               "activity": "administered activity (NM) is not collected yet"}


class DRLError(ValueError):
    pass


def reference_sets(session: Session) -> list[dict]:
    out = []
    for rs in session.scalars(select(ReferenceSet).order_by(ReferenceSet.key)):
        out.append({"key": rs.key, "name": rs.name, "source": rs.source, "notes": rs.notes,
                    "valid_from": rs.valid_from.isoformat() if rs.valid_from else None,
                    "values": len(rs.values)})
    return out


def compare(session: Session, conds: list, set_key: str, *, by_device: bool = False,
            min_n: int = 20, physical: dict[int, int] | None = None,
            names: dict[int, str] | None = None) -> dict:
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

    # one query per quantity: (value, study, device, age, weight, exam code id)
    by_q: dict[str, list[ReferenceValue]] = defaultdict(list)
    for rv, *_ in refs:
        by_q[rv.quantity].append(rv)
    rows_by_q: dict[str, list] = {}
    for quantity, rvs in by_q.items():
        if quantity not in QUANTITY:
            continue
        col, _units = QUANTITY[quantity]
        code_ids = {rv.exam_code_id for rv in rvs}
        c = aliased(Classification)
        link = and_(c.study_id == Study.id, c.code_id.in_(code_ids),
                    or_(c.event_id == Event.id,
                        and_(c.event_id.is_(None), Study.modality.not_in(EXPOSURE_EXAM),
                             ~own_codes(Event.id))))
        q = (select(col, Event.id, Study.id, Study.device_id, Study.age_days, Study.weight_kg,
                    c.code_id)
             .select_from(Event).join(Study, Study.id == Event.study_id).join(c, link)
             .where(col.is_not(None), Event.is_localizer.is_not(True), *conds))
        rows_by_q[quantity] = session.execute(q).all()

    bands: dict[str, BandLookup] = {}
    results = []
    for rv, code, band, scheme in refs:
        item = {"exam": code.code, "exam_meaning": code.meaning,
                "band": band.key if band else None, "band_label": band.label if band else None,
                "scheme": scheme.key if scheme else None, "quantity": rv.quantity,
                "applies_to": rv.applies_to, "reference": rv.value, "unit": rv.unit}
        if rv.quantity not in QUANTITY:
            results.append({**item, "status": "unavailable",
                            "reason": UNAVAILABLE.get(rv.quantity, "not supported"),
                            "groups": []})
            continue
        factor = QUANTITY[rv.quantity][1].get(rv.unit)
        if factor is None:
            results.append({**item, "status": "unavailable",
                            "reason": f"unit {rv.unit} not supported", "groups": []})
            continue
        lookup = None
        if scheme is not None:
            lookup = bands.get(scheme.key) or bands.setdefault(
                scheme.key, BandLookup(session, scheme.key, scheme.parameter))
        units: dict[tuple, float] = defaultdict(float)  # (unit id, device) -> value
        for value, eid, sid, did, age, weight, cid in rows_by_q[rv.quantity]:
            if cid != rv.exam_code_id:
                continue
            if lookup is not None:
                key = lookup(age if lookup.parameter == "age" else weight)
                if key != band.key:
                    continue
            dev = physical.get(did, did) if did is not None else None
            unit_id = sid if rv.applies_to == "study" else eid
            units[(unit_id, dev if by_device else None)] += value * factor
        per_group: dict = defaultdict(list)
        for (_uid, dev), v in units.items():
            per_group[dev].append(v)
        groups = []
        for dev, vals in per_group.items():
            d = describe(vals)
            groups.append({"device": dev, "device_name": names.get(dev) if dev else None,
                           **d, "ratio": d["median"] / rv.value if rv.value else None,
                           "status": _status(d, rv.value, min_n)})
        groups.sort(key=lambda g: (g["device_name"] or "", -g["n"]))
        results.append({**item, "groups": groups,
                        "status": _worst([g["status"] for g in groups]) if groups else "no_data"})
    results.sort(key=lambda r: (r["exam"], r["quantity"], _band_order(r, bands)))
    return {"set": rs.key, "name": rs.name, "source": rs.source, "notes": rs.notes,
            "by": "device" if by_device else "facility", "min_n": min_n, "rows": results}


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
