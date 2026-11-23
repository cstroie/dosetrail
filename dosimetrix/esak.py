"""Entrance-surface air kerma (ESAK) for rooms without a DAP meter: Order MS 1255/2016 art. 5
and annex 2.

    ESAK = Y(kVp, FDD) x mAs x [FDD / (FFD - T)]^2 x BSF

* **Y**: the tube output (mGy/mAs) at FDD (50 cm), read from the device's own output curve
  (``device_output``: measured by the medical physicist, annex 2 point 2). Linear between the
  measured kVp values; outside them nothing is calculated.
* **FFD**: the focus to film or detector distance, from the event's source-image distance.
* **T**: the patient's thickness, ``sqrt(4 G / (rho x pi x H))`` with G the weight in kg, H the
  height in cm and rho = 1e-3 kg/cm3 (annex 2 point 3 b). Needs the study's weight and height.
* **BSF**: the back-scatter factor of annex 2 table 2, by kVp (60 to 120, a 3 mm Al filter) and
  field size (10 to 30 cm), interpolated and held at the table's edge outside it. Without a
  field size the annex's 1.3 to 1.4 is used: 1.35, method ``annex2_bsf``.

Only events with kVp, mAs, a distance, a size, a curve and **no DAP** are calculated: a room with
a DAP meter reports DAP, as the norm intends. The device's own entrance dose (``event.esd``)
is never replaced; the result goes to ``event.esak_calc`` with ``esak_method``.
"""

from __future__ import annotations

import bisect
import math

RHO = 1e-3  # kg/cm3, annex 2
BSF_KVP = (60, 70, 80, 90, 100, 120)
BSF_FIELD_CM = (10, 15, 20, 30)
BSF_TABLE = {  # annex 2 table 2: rows by field size, columns by BSF_KVP
    10: (1.27, 1.29, 1.30, 1.33, 1.37, 1.42),
    15: (1.30, 1.32, 1.34, 1.37, 1.40, 1.45),
    20: (1.31, 1.33, 1.35, 1.38, 1.41, 1.46),
    30: (1.33, 1.35, 1.37, 1.40, 1.43, 1.48),
}
BSF_DEFAULT = 1.35


def thickness_cm(weight_kg: float, height_m: float) -> float:
    return math.sqrt(4 * weight_kg / (RHO * math.pi * height_m * 100))


def _lerp(xs: tuple | list, ys: tuple | list, x: float) -> float:
    """Linear interpolation, held at the ends."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, x)
    t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
    return ys[i - 1] + t * (ys[i] - ys[i - 1])


def bsf(kvp: float, field_side_cm: float | None) -> tuple[float, str]:
    if not field_side_cm:
        return BSF_DEFAULT, "annex2_bsf"
    by_kvp = [_lerp(BSF_KVP, BSF_TABLE[f], kvp) for f in BSF_FIELD_CM]
    return _lerp(BSF_FIELD_CM, by_kvp, field_side_cm), "annex2"


def output_at(curve: list[tuple[float, float]], kvp: float) -> float | None:
    """Y(kVp) from the measured points (sorted); None outside the measured range."""
    if not curve or kvp < curve[0][0] or kvp > curve[-1][0]:
        return None
    return _lerp([p[0] for p in curve], [p[1] for p in curve], kvp)


def esak(kvp, mas, ffd_cm, weight_kg, height_m, curve, fdd_cm, field_w_mm=None,
         field_h_mm=None) -> tuple[float, str] | None:
    """(ESAK mGy, method) or None when something the formula needs is missing."""
    if not (kvp and mas and ffd_cm and weight_kg and height_m):
        return None
    y = output_at(curve, kvp)
    if y is None:
        return None
    t = thickness_cm(weight_kg, height_m)
    if ffd_cm <= t:
        return None
    side = math.sqrt(field_w_mm * field_h_mm) / 10 if field_w_mm and field_h_mm else None
    factor, method = bsf(kvp, side)
    return round(y * mas * (fdd_cm / (ffd_cm - t)) ** 2 * factor, 4), method


def curves(session) -> dict[int, tuple[list[tuple[float, float]], float]]:
    """physical device id -> (sorted [(kVp, mGy/mAs)], FDD cm)."""
    from sqlalchemy import select

    from .db.models import DeviceOutput

    out: dict[int, tuple[list, float]] = {}
    for r in session.scalars(select(DeviceOutput).order_by(DeviceOutput.device_id,
                                                           DeviceOutput.kvp)):
        pts, _ = out.setdefault(r.device_id, ([], r.fdd_cm))
        pts.append((r.kvp, r.mgy_per_mas))
    return out


def update(session, study_ids=None) -> int:
    """Recalculate esak_calc for X-ray events (all, or of the given studies). Returns changes."""
    from sqlalchemy import or_, select

    from .db.models import Device, Event, Study

    cv = curves(session)
    merged = dict(session.execute(select(Device.id, Device.merged_into_id)).all())
    q = (select(Event, Study).join(Study, Study.id == Event.study_id)
         .where(Study.modality.in_(("DX", "CR")),
                or_(Event.event_type.is_(None), Event.event_type != "fluoro")))
    if study_ids is not None:
        q = q.where(Event.study_id.in_(list(study_ids)))
    n = 0
    for ev, st in session.execute(q):
        res = None
        curve = cv.get(merged.get(st.device_id) or st.device_id)
        if curve and ev.dap is None:
            mas = ev.mas or (ev.ma * ev.exposure_time_s if ev.ma and ev.exposure_time_s else None)
            res = esak(ev.kvp, mas, (ev.sid_mm or 0) / 10, st.weight_kg, st.height_m, curve[0],
                       curve[1], ev.field_w_mm, ev.field_h_mm)
        val, method = res or (None, None)
        if (ev.esak_calc, ev.esak_method) != (val, method):
            ev.esak_calc, ev.esak_method = val, method
            n += 1
    session.flush()
    return n
