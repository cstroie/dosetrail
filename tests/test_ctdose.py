"""CT effective dose by the DLP method: coefficients by region, age and phantom."""

import datetime as dt
import io

import pytest

from dozimetrix import classify, ctdose
from dozimetrix.api import data
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import effective
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
ROWS = [("head", 3.0, 16, 0.005, "test set"), ("head", None, 16, 0.002, "test set"),
        ("chest", 10.0, 16, 0.02, "test set"), ("chest", None, 16, 0.016, "test set"),
        ("chest", None, 32, 0.017, "test set")]


def test_table_lookup_by_age_and_phantom():
    t = ctdose.KTable(ROWS)
    yr = 365.25
    assert t.get("head", int(2 * yr), 16) == (0.005, "test set")
    assert t.get("head", int(3.5 * yr), 16) == (0.002, "test set")  # the adult row
    assert t.get("chest", int(30 * yr), 32) == (0.017, "test set")
    assert t.get("chest", int(5 * yr), 32) is None  # no 32 cm row for children: no guess
    assert t.get("neck", 100, 16) is None and t.get("head", None, 16) is None
    assert t.get("head", 100, None) is None and not ctdose.KTable([])


def test_set_rows_validates(session):
    assert ctdose.set_rows(session, ROWS) == 5
    assert len(ctdose.load(session)._by) == 3
    for bad in ([("knee", None, 16, 0.01, "x")], [("head", None, 20, 0.01, "x")],
                [("head", None, 16, 2.0, "x")], [("head", None, 16, 0.01, " ")],
                [("head", 0.0, 16, 0.01, "x")],
                [("head", None, 16, 0.01, "x"), ("head", None, 16, 0.02, "y")]):
        with pytest.raises(ctdose.CtDoseError):
            ctdose.set_rows(session, bad)
    assert ctdose.set_rows(session, []) == 0 and not ctdose.load(session)


@pytest.fixture
def world(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="cd-a", is_local=True)
    session.add(dev)
    session.flush()

    def study(uid, desc, age_years, dlp, phantom=16):
        s = Study(study_uid=uid, modality="CT", device_id=dev.id, study_datetime=T,
                  study_description=desc, source_kind="test", age_days=int(age_years * 365.25),
                  total_dlp=dlp)
        s.events = [Event(protocol="Topogram", is_localizer=True, dlp=5.0),
                    Event(protocol="Spiral", dlp=dlp, ctdivol=dlp / 15, ct_phantom_cm=phantom)]
        session.add(s)
        session.flush()
        return s

    return study


def test_estimate_and_the_study_page(session, world):
    head = world("cd.1", "Head^CAP_SPIRAL_NATIV (Child)", 2.0, 600.0)
    chest32 = world("cd.2", "Torace^TORACE_SPIRAL (Child)", 6.0, 300.0, phantom=32)
    classify.run(session)
    assert data.study(session, head.id)["totals"]["effective_dose"] is None  # no coefficients
    est = ctdose.for_study(session, head.id)
    assert est["msv"] is None and "no conversion coefficients" in est["reason"]
    ctdose.set_rows(session, ROWS)
    est = ctdose.for_study(session, head.id)
    assert est["msv"] == pytest.approx(3.0) and est["region"] == "head" and est["complete"]
    shown = data.study(session, head.id)["totals"]["effective_dose"]
    assert shown["msv"] == 3.0 and shown["sources"] == ["test set"]
    assert "not for an individual patient" in shown["note"]
    # a child scanned with the 32 cm phantom: no 32 cm coefficient for children, so no guess
    est = ctdose.for_study(session, chest32.id)
    assert est["msv"] is None and "phantom 32" in est["reason"]
    assert ctdose.for_study(session, 999999) is None


def test_report_groups_and_counts_what_has_no_estimate(session, world):
    for i in range(3):
        world(f"cd.h{i}", "Head^CAP_SPIRAL_NATIV (Child)", 2.0, 600.0 + 100 * i)
    world("cd.c", "Torace^TORACE_SPIRAL (Child)", 6.0, 300.0, phantom=32)  # no coefficient
    classify.run(session)
    ctdose.set_rows(session, ROWS)
    rep = effective.build(session, [])
    assert (rep["studies"], rep["estimated"], rep["coefficients"]) == (4, 3, True)
    (g,) = rep["groups"]
    assert g["exam"] == "CT-CAP" and g["n"] == 3 and g["median_msv"] == 3.5
    assert g["total_msv"] == pytest.approx(10.5)
    assert rep["rooms"][0]["collective_msv"] == pytest.approx(10.5)
    assert rep["not_estimated"][0]["n"] == 1 and "phantom" in rep["not_estimated"][0]["reason"]
    out = io.BytesIO()
    effective.write_csv(rep, out)
    assert out.getvalue().decode("utf-8-sig").startswith("exam,age_group")
