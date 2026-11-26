"""Collimation audit: field area = DAP / air kerma, per room against the department."""

import datetime as dt

from dozimetrix import classify
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import collimation
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
AGE = int(8 * 365.25)
# (protocol, view) of three exams; area_cm2 = DAP / kerma
EXAMS = [("W001a Craniu a.p.", "antero-posterior"), ("W001a Craniu lat.", "lateral"),
         ("W035 Torace a.p.", "antero-posterior"), ("W040 Abdomen a.p.", "antero-posterior")]


def _room(session, name, factor_by_exam, n=12):
    dev = Device(display_name=name, identity_key=f"co-{name}", is_local=True)
    session.add(dev)
    session.flush()
    for i in range(n):
        s = Study(study_uid=f"{name}.{i}", modality="DX", device_id=dev.id, study_datetime=T,
                  study_description="Rx", source_kind="test", age_days=AGE)
        s.events = [Event(protocol=p, view=v, dap=0.4 * f, rp_dose=0.001 * 1.0)
                    for (p, v), f in zip(EXAMS, factor_by_exam, strict=True)]
        session.add(s)


def test_uniform_error_is_told_from_collimation(session):
    load_all(session)
    for name in ("A", "B", "C"):
        _room(session, name, [1.0, 1.0, 1.0, 1.0])  # 400 cm² everywhere: the department
    _room(session, "Calib", [2.0, 2.0, 2.0, 2.0])  # every exam doubled: a calibration factor
    _room(session, "Open", [1.0, 1.0, 1.0, 2.0])  # one exam with the field open
    session.flush()
    classify.run(session)
    rep = collimation.build(session, [])
    got = {r["device"]: r for r in rep["rooms"]}
    exams = {g["exam"] for g in got["A"]["detail"]}
    assert len(exams) >= 3, exams  # the national codes were found
    assert got["A"]["verdict"] == "ok" and got["A"]["median_ratio"] <= 1.0
    assert got["Calib"]["verdict"] == "uniform_high"
    assert got["Open"]["verdict"] == "collimation" and got["Open"]["high_groups"] >= 1
    assert rep["rooms"][0]["device"] == "Calib"  # the worst first
    assert {g["n"] for g in got["A"]["detail"]} == {12}
