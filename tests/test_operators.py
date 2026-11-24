"""Operator dose report: each value against the department median for its exam and age."""

import datetime as dt

from dozimetrix import classify
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import operators
from dozimetrix.seeds.loader import load_all

AGE_2Y = int(2 * 365.25) + 30
DAY = dt.datetime(2026, 8, 1)


def test_operator_name_spellings():
    assert operators.operator("POP^ANA ") == operators.operator("pop  ana") == "POP ANA"
    assert operators.operator("  ") is None and operators.operator(None) is None


def test_operators_against_the_department(session):
    load_all(session)
    ct = Device(display_name="CT room", identity_key="op-1", is_local=True)
    session.add(ct)
    session.flush()
    n = 0

    def study(op, dlp, count):
        nonlocal n
        for _ in range(count):
            n += 1
            s = Study(study_uid=f"op.{n}", modality="CT", device_id=ct.id, source_kind="test",
                      study_datetime=DAY + dt.timedelta(hours=n), age_days=AGE_2Y,
                      study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)", total_dlp=dlp,
                      operator_name=op)
            s.events = [Event(protocol="Head", dlp=dlp)]
            session.add(s)

    study("POP^ANA", 400.0, 30)
    study("ION^DAN", 640.0, 22)
    study("NEW^ONE", 800.0, 3)  # too few values to be flagged
    study(None, 400.0, 45)      # no operator: counts for the median and coverage only
    session.flush()
    classify.run(session)
    rep = operators.build(session, [], dt.date(2026, 7, 1), dt.date(2026, 9, 30))
    by = {o["operator"]: o for o in rep["operators"]}
    assert by["ION DAN"]["median_ratio"] == 1.6 and by["ION DAN"]["flag"]
    assert by["POP ANA"]["median_ratio"] == 1.0 and not by["POP ANA"]["flag"]
    assert by["NEW ONE"]["share_p95"] == 1.0 and not by["NEW ONE"]["flag"]
    assert [o["operator"] for o in rep["operators"]] == ["ION DAN", "POP ANA", "NEW ONE"]
    assert by["ION DAN"]["exams"][0]["exam"] == "CT-CAP"
    assert rep["coverage"] == [{"device": "CT room", "studies": 100, "with_operator": 55,
                                "share": 0.55}]
