"""QA and calibration register: factors by date, reminders, the study page and collimation."""

import datetime as dt

import pytest
from sqlalchemy import select

from dozimetrix import alerts, qa
from dozimetrix.api import data
from dozimetrix.db.models import Alert, AlertRule, Device, Event, Patient, Study
from dozimetrix.seeds.loader import load_all

NOW = dt.datetime(2026, 11, 26, 9, 0)
D = dt.date


@pytest.fixture
def world(session):
    load_all(session)
    dev = Device(display_name="Ysio", identity_key="qa-1", is_local=True)
    pat = Patient(pseudo_id="P-QA1", sex="M")
    session.add_all([dev, pat])
    session.flush()
    return dev, pat


def test_validation(session, world):
    dev, _ = world
    with pytest.raises(qa.QAError, match="factor"):
        qa.add(session, dev.id, "u", kind="dap_calibration", done_on=D(2026, 5, 1))
    with pytest.raises(qa.QAError, match="0.5"):
        qa.add(session, dev.id, "u", kind="dap_calibration", done_on=D(2026, 5, 1), factor=3.0)
    with pytest.raises(qa.QAError, match="only a DAP"):
        qa.add(session, dev.id, "u", kind="annual", done_on=D(2026, 5, 1), factor=1.1)
    with pytest.raises(qa.QAError, match="kind"):
        qa.add(session, dev.id, "u", kind="x", done_on=D(2026, 5, 1))
    with pytest.raises(qa.QAError, match="after"):
        qa.add(session, dev.id, "u", kind="annual", done_on=D(2026, 5, 1), next_due=D(2026, 5, 1))
    with pytest.raises(qa.QAError, match="device"):
        qa.add(session, 9999, "u", kind="annual", done_on=D(2026, 5, 1))
    with pytest.raises(qa.QAError, match="entry"):
        qa.remove(session, 9999)


def test_factor_applies_from_its_date_until_the_next(session, world):
    dev, _ = world
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=D(2026, 3, 1), factor=1.12)
    e2 = qa.add(session, dev.id, "u", kind="dap_calibration", done_on=D(2026, 9, 1), factor=0.95)
    f = qa.Factors(session)
    assert f.at(dev.id, dt.datetime(2026, 2, 28, 23)) == 1.0  # before the first
    assert f.at(dev.id, dt.datetime(2026, 3, 1, 8)) == 1.12
    assert f.at(dev.id, dt.datetime(2026, 8, 31)) == 1.12
    assert f.at(dev.id, dt.datetime(2026, 11, 1)) == 0.95
    assert f.at(None, NOW) == 1.0 and f.at(dev.id, None) == 1.0
    qa.remove(session, e2.id)  # a wrong entry is corrected by editing the register
    assert qa.factor_at(session, dev.id, dt.datetime(2026, 11, 1)) == 1.12


def test_study_page_and_dose_statement_show_the_corrected_dap(session, world):
    dev, pat = world
    qa.add(session, dev.id, "u", kind="dap_calibration", done_on=D(2026, 3, 1), factor=1.25)
    s = Study(study_uid="qa.1", device_id=dev.id, patient_id=pat.id, modality="RF",
              study_datetime=NOW, source_kind="test", total_dap=0.8)
    s.events = [Event(event_type="fluoro", dap=0.8)]
    session.add(s)
    session.flush()
    t = data.study(session, s.id)["totals"]
    assert t["dap_gycm2"] == 0.8 and t["dap_factor"] == 1.25
    assert t["dap_corrected_gycm2"] == pytest.approx(1.0)
    dap = next(d for d in data.dose_statement(session, s.id)["doses"] if d["key"] == "dap")
    assert dap["value"] == pytest.approx(1000.0) and "× 1.25" in dap["note"]
    other = Study(study_uid="qa.2", device_id=dev.id, patient_id=pat.id, modality="RF",
                  study_datetime=dt.datetime(2026, 1, 1), source_kind="test", total_dap=0.8)
    session.add(other)
    session.flush()
    assert data.study(session, other.id)["totals"]["dap_factor"] is None  # before the calibration


def test_due_and_reminder_alert(session, world):
    dev, pat = world
    s = Study(study_uid="qa.3", device_id=dev.id, patient_id=pat.id, modality="DX",
              study_datetime=NOW - dt.timedelta(days=1), source_kind="test")
    session.add(s)
    session.flush()
    qa.add(session, dev.id, "boss", kind="annual", done_on=D(2025, 11, 1), next_due=D(2026, 11, 1))
    qa.add(session, dev.id, "boss", kind="constancy", done_on=D(2026, 11, 10),
           next_due=D(2026, 12, 10))  # due in 14 days
    qa.add(session, dev.id, "boss", kind="acceptance", done_on=D(2026, 6, 1),
           next_due=D(2027, 6, 1))  # far
    qa.add(session, dev.id, "boss", kind="dap_calibration", done_on=D(2025, 6, 1), factor=1.0,
           next_due=D(2026, 6, 1))
    qa.add(session, dev.id, "boss", kind="dap_calibration", done_on=D(2026, 6, 1), factor=1.0,
           next_due=D(2027, 6, 1))  # renewed: the old entry no longer counts
    rows = qa.due(session, NOW.date(), names={dev.id: "Ysio"})
    assert [(r["kind"], r["days"], r["overdue"]) for r in rows] == [
        ("annual", -25, True), ("constancy", 14, False)]
    assert alerts.evaluate_devices(session, NOW, ["qa_due"])["new"] == 1  # one per device
    (a,) = session.scalars(select(Alert).join(AlertRule).where(AlertRule.key == "qa-due-30d"))
    assert a.message == ("Ysio: annual check overdue by 25 days (due 2026-11-01); "
                         "constancy test due in 14 days (due 2026-12-10)")
    assert a.value == -25.0
    row = alerts.listing(session)[0]
    assert (row["kind"], row["value"]) == ("QA", "25 d overdue")
    # once per window; again after 30 days
    assert alerts.evaluate_devices(session, NOW + dt.timedelta(days=2), ["qa_due"])["new"] == 0
    later = NOW + dt.timedelta(days=31)
    session.add(Study(study_uid="qa.4", device_id=dev.id, patient_id=pat.id, modality="DX",
                      study_datetime=later - dt.timedelta(hours=2), source_kind="test"))
    session.flush()
    assert alerts.evaluate_devices(session, later, ["qa_due"])["new"] == 1
