"""Collimation audit for radiography: the exposed field area, DAP ÷ air kerma at the reference
point, per room against the department for the same exam and patient group (ICRP 135 / RP 185:
head by age, the rest by weight).

DAP (Gy·cm²) is the air kerma (Gy) integrated over the beam's cross-section, so DAP ÷ Ka is the
field area at the reference point (cm²). Two different faults give the same excess DAP and are
told apart by their pattern:

* **over-collimation**: the field is too large for particular exams (a hand shot with the field
  open to the whole detector): a few exam and patient groups of the room are high, the others are
  normal;
* **a DAP meter (or kerma) calibration error**: every exam of the room is off by the same factor,
  because the factor scales every DAP, whatever the field.

Patients are grouped as in ICRP 135 / RP 185 (extended): head exams by age, the others by
weight (a weight estimated from the growth charts counts as unknown), not by the national age
groups. A room is judged on its exam and patient groups that have enough values both in the
room and in the department (≥ 10 and ≥ 20). A DAP meter calibration factor recorded in the QA
register is applied to the DAP first, so a room whose meter was calibrated no longer shows as a
calibration suspect. The kerma is the one the device reports with the exposure: where it is
calculated from the DAP and a nominal field, the
area is not independent of the DAP and the room will look normal.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from typing import IO

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from ..db.models import Classification, Code, CodeSystem, Event, SizeSource, Study
from ..grid import Grid
from ..qa import Factors
from ..stats import quantile

NATIONAL = "ro-drl-ms838-2025"
MIN_ROOM, MIN_DEPT = 10, 20
HIGH, LOW = 1.5, 1 / 1.5  # a group is high (low) at 1.5 × the department's median area
UNIFORM_SHARE = 0.7  # of a room's groups off in the same direction: a calibration error
MIN_GROUPS_UNIFORM = 3
AREA_MIN, AREA_MAX = 5.0, 3000.0  # cm²: outside, the kerma or DAP is a unit or reading error


def _median(v: list[float]) -> float:
    return quantile(sorted(v), 0.5)


def build(session: Session, conds: list, min_room: int = MIN_ROOM,
          min_dept: int = MIN_DEPT) -> dict:
    from ..api.data import device_names

    system = session.scalar(select(CodeSystem.id).where(CodeSystem.key == NATIONAL))
    names = device_names(session)
    grid = Grid(session)
    cells: dict[tuple, list[float]] = defaultdict(list)  # (room, code, band) -> areas
    factors = Factors(session)  # DAP meter calibration from the QA register (1 without one)
    weight = case((Study.weight_source == SizeSource.estimated_reference, None),
                  else_=Study.weight_kg)
    rows = session.execute(
        select(Study.device_id, Code.code, Study.age_days, weight, Event.dap, Event.rp_dose,
               Study.study_datetime)
        .select_from(Event).join(Study, Study.id == Event.study_id)
        .join(Classification, Classification.event_id == Event.id)
        .join(Code, (Code.id == Classification.code_id) & (Code.system_id == system))
        .where(Study.modality.in_(("DX", "CR")), Event.dap > 0, Event.rp_dose > 0, *conds))
    for device_id, code, age, wt, dap, ka, when in rows:
        b = grid.group(NATIONAL, code, age, wt)
        area = dap * factors.at(device_id, when) / ka
        if b not in ("?", "age:?", "weight:?") and AREA_MIN <= area <= AREA_MAX:
            cells[(device_id, code, b)].append(area)

    dept: dict[tuple, list[float]] = defaultdict(list)
    for (_, code, b), v in cells.items():
        dept[(code, b)].extend(v)
    ref = {k: _median(v) for k, v in dept.items() if len(v) >= min_dept}

    rooms: dict[int, list[dict]] = defaultdict(list)
    for (device_id, code, b), v in cells.items():
        if len(v) < min_room or (code, b) not in ref:
            continue
        m = _median(v)
        rooms[device_id].append({"exam": code, "band": grid.labels.get(b, b), "n": len(v),
                                 "median_area_cm2": round(m),
                                 "dept_area_cm2": round(ref[(code, b)]),
                                 "ratio": round(m / ref[(code, b)], 2)})
    out = []
    for device_id, groups in rooms.items():
        groups.sort(key=lambda g: -g["ratio"])
        ratios = [g["ratio"] for g in groups]
        n = len(groups)
        high = [g for g in groups if g["ratio"] >= HIGH]
        n_off_high = sum(r >= 1.3 for r in ratios)
        n_off_low = sum(r <= 1 / 1.3 for r in ratios)
        med = _median(ratios)
        if n >= MIN_GROUPS_UNIFORM and n_off_high >= UNIFORM_SHARE * n and med >= 1.3:
            verdict = "uniform_high"  # all exams large: DAP or kerma calibration
        elif n >= MIN_GROUPS_UNIFORM and n_off_low >= UNIFORM_SHARE * n and med <= 1 / 1.3:
            verdict = "uniform_low"
        elif high:
            verdict = "collimation"  # particular exams
        else:
            verdict = "ok"
        out.append({"device": names.get(device_id, f"Device {device_id}"), "verdict": verdict,
                    "groups": n, "median_ratio": round(med, 2), "high_groups": len(high),
                    "exposures": sum(g["n"] for g in groups), "detail": groups})
    order = {"uniform_high": 0, "uniform_low": 1, "collimation": 2, "ok": 3}
    out.sort(key=lambda r: (order[r["verdict"]], -r["median_ratio"], r["device"]))
    return {"min_room": min_room, "min_dept": min_dept, "high": HIGH, "rooms": out}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["room", "verdict", "exam", "patient_group", "n", "median_area_cm2",
                "department_area_cm2", "ratio"])
    for r in rep["rooms"]:
        for g in r["detail"]:
            w.writerow([r["device"], r["verdict"], g["exam"], g["band"], g["n"],
                        g["median_area_cm2"], g["dept_area_cm2"], g["ratio"]])
    text.detach()
