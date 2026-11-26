"""The DAP meter's calibration (QA register) in statistics, DRL, exports, alerts and follow-ups:
a DAP is the stored one times the factor of its device on its date; nothing else changes."""

import datetime as dt

import pytest
from sqlalchemy import select

from dozimetrix import alerts, classify, drl, followup, qa, stats
from dozimetrix.api import data
from dozimetrix.db.models import Alert, AlertRule, Device, Event, Patient, Study
from dozimetrix.export import lists
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
AGE = int(8 * 365.25)


@pytest.fixture
def world(session):
    load_all(session)
    dev = Device(display_name="Ysio", identity_key="qp-1", is_local=True)
    pat = Patient(pseudo_id="P-QP1", sex="M")
    session.add_all([dev, pat])
    session.flush()

    def rx(uid, when, dap):
        s = Study(study_uid=uid, device_id=dev.id, patient_id=pat.id, modality="DX",
                  study_datetime=when, study_description="Torace", source_kind="test",
                  age_days=AGE, total_dap=dap)
        s.events = [Event(protocol="W035 Torace a.p.", view="antero-posterior", dap=dap)]
        session.add(s)
        session.flush()
        return s

    return dev, rx


def test_stats_use_the_corrected_dap_from_the_calibration_date(session, world):
    dev, rx = world
    before = rx("a", dt.datetime(2026, 2, 1), 0.10)
    after = rx("b", T, 0.10)
    ids = [before.id, after.id]
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=dt.date(2026, 3, 1), factor=1.25)

    def values(metric):
        res = stats.compute(session, [Study.id.in_(ids)], metric, ["device"])
        return sorted(round(v, 6) for g in res["groups"] for v in [g["max"], g["min"]])

    assert values("dap") == [0.1, 0.125]  # before the calibration: stored; after: × 1.25
    assert values("dap_total") == [0.1, 0.125]
    assert stats.compute(session, [Study.id.in_(ids)], "kar_total", [])  # other metrics untouched


def test_drl_comparison_uses_the_corrected_dap(session, world):
    dev, rx = world
    for i in range(6):
        rx(f"d{i}", T + dt.timedelta(hours=i), 0.10)
    classify.run(session)
    ids = [s.id for s in session.scalars(select(Study))]
    items = [drl.Item(key=0, exam_code_id=session.scalar(select(drl.Code.id).where(
        drl.Code.code == "RX-TORACE-PA-AP")), quantity="dap", unit="Gy.cm2", applies_to="exposure")]
    got = drl.collect(session, [Study.id.in_(ids)], items)[0]
    assert sorted(got[None]) == pytest.approx([0.1] * 6)
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=dt.date(2026, 3, 1), factor=0.8)
    got = drl.collect(session, [Study.id.in_(ids)], items)[0]
    assert sorted(got[None]) == pytest.approx([0.08] * 6)


def test_exports_say_which_factor_they_applied(session, world):
    dev, rx = world
    s = rx("e", T, 0.10)
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=dt.date(2026, 3, 1), factor=1.25)
    cols = [c for c, _ in lists.columns("study", False)]
    row = next(lists.rows(session, [Study.id == s.id]))
    assert row[cols.index("DAP total [mGy.cm2]")] == pytest.approx(125.0)
    assert row[cols.index("DAP correction factor")] == 1.25
    ecols = [c for c, _ in lists.columns("event", False)]
    erow = next(lists.rows(session, [Study.id == s.id], level="event"))
    assert erow[ecols.index("DAP [mGy.cm2]")] == pytest.approx(125.0)
    assert erow[ecols.index("DAP correction factor")] == 1.25


def test_chart_rows_and_alert_thresholds_use_it(session, world):
    dev, rx = world
    s = rx("f", T, 0.40)  # Gy·cm²
    fl = Study(study_uid="g", device_id=dev.id, modality="XA", study_datetime=T,
               source_kind="test", total_dap=420.0, age_days=20000)
    session.add(fl)
    session.flush()
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=dt.date(2026, 3, 1), factor=1.25)
    rows = data.group_rows(session, "origin:local", T + dt.timedelta(days=1), "DX")
    assert rows[0]["dap"] == pytest.approx(0.5)
    # the NCRP level 500 Gy·cm² is on the true DAP: 420 stored is 525 corrected
    alerts.evaluate(session, [fl.id, s.id])
    got = {r.key for r in session.scalars(select(AlertRule).join(Alert).where(
        Alert.study_id == fl.id))}
    assert "fluoro-dap-500" in got


def test_followup_level_uses_the_corrected_kap(session, world):
    dev, _ = world
    fl = Study(study_uid="h", device_id=dev.id, modality="XA", study_datetime=T,
               source_kind="test", total_dap=420.0, age_days=20000)
    session.add(fl)
    session.flush()
    assert followup.levels(fl) is None
    assert followup.sync(session, [fl.id])["new"] == 0
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=dt.date(2026, 3, 1), factor=1.25)
    assert followup.levels(fl, 1.25) == "KAP 525000 mGy·cm² ≥ 500000"
    assert followup.sync(session, [fl.id])["new"] == 1
    assert followup.sync(session)["new"] == 0  # the all-studies pass finds the same one
