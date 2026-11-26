"""Incident records: opening from an alert, the audit log, closing rules."""

import datetime as dt

import pytest
from sqlalchemy import select

from dozimetrix import alerts, incidents
from dozimetrix.db.models import Alert, Device, Patient, Study
from dozimetrix.seeds.loader import load_all

NOW = dt.datetime(2026, 11, 26, 9, 0)


@pytest.fixture
def alert(session):
    load_all(session)
    dev = Device(display_name="Angio", identity_key="in-1", is_local=True)
    pat = Patient(pseudo_id="P-IN1", sex="M")
    session.add_all([dev, pat])
    session.flush()
    s = Study(study_uid="in.1", device_id=dev.id, patient_id=pat.id, study_datetime=NOW,
              source_kind="test", modality="XA", total_rp_dose=6.0, age_days=20000)
    session.add(s)
    session.flush()
    alerts.evaluate(session, [s.id])
    return session.scalars(select(Alert).where(Alert.study_id == s.id)).first(), s


def _t(minutes=0):
    return NOW + dt.timedelta(minutes=minutes)


def test_open_from_alert_and_one_open_per_alert(session, alert):
    a, s = alert
    inc = incidents.open_incident(session, "boss", _t(), alert_id=a.id)
    assert inc.study_id == s.id and inc.title.startswith("Fluoroscopy")
    assert [e.kind for e in inc.log] == ["opened"]
    with pytest.raises(incidents.IncidentError, match="already open"):
        incidents.open_incident(session, "boss", _t(), alert_id=a.id)
    with pytest.raises(incidents.IncidentError, match="alert or a study"):
        incidents.open_incident(session, "boss", _t())
    with pytest.raises(incidents.IncidentError, match="title"):
        incidents.open_incident(session, "boss", _t(), study_id=s.id)
    other = incidents.open_incident(session, "boss", _t(), study_id=s.id, title="Wrong patient")
    assert other.alert_id is None and other.study_id == s.id


def test_changes_are_logged_and_closing_has_rules(session, alert):
    a, _ = alert
    inc = incidents.open_incident(session, "boss", _t(), alert_id=a.id)
    incidents.update(session, inc.id, "boss", _t(1), cause="long case, steep angulation")
    incidents.update(session, inc.id, "boss", _t(2), cause="long case, steep angulation")  # same
    incidents.update(session, inc.id, "phys", _t(3), actions="operator briefed")
    incidents.note(session, inc.id, "phys", _t(4), "  talked to the cardiologist ")
    assert [(e.username, e.kind, e.text) for e in inc.log[1:]] == [
        ("boss", "change", "cause: — → long case, steep angulation"),
        ("phys", "change", "actions: — → operator briefed"),
        ("phys", "note", "talked to the cardiologist")]
    with pytest.raises(incidents.IncidentError, match="outcome"):
        incidents.close(session, inc.id, "boss", _t(5))
    incidents.update(session, inc.id, "boss", _t(5), outcome="no skin effect at the check")
    with pytest.raises(incidents.IncidentError, match="reportable"):
        incidents.close(session, inc.id, "boss", _t(6))
    incidents.update(session, inc.id, "boss", _t(6), reportable=True,
                     reportable_basis="above 5 Gy without a clinical reason")
    with pytest.raises(incidents.IncidentError, match="reference"):
        incidents.close(session, inc.id, "boss", _t(7))
    incidents.update(session, inc.id, "boss", _t(7), reported_at=_t(7),
                     report_reference="CNCAN/123")
    incidents.close(session, inc.id, "boss", _t(8))
    assert inc.status == "closed" and inc.closed_at == _t(8)
    with pytest.raises(incidents.IncidentError, match="closed"):
        incidents.update(session, inc.id, "boss", _t(9), cause="x")
    incidents.reopen(session, inc.id, "boss", _t(10), "new information")
    assert inc.status == "open" and inc.closed_at is None and inc.log[-1].kind == "reopened"
    # a closed incident of an alert can have a second one opened later
    incidents.close(session, inc.id, "boss", _t(11))
    assert incidents.open_incident(session, "boss", _t(12), alert_id=a.id).id != inc.id


def test_listing_and_detail(session, alert):
    a, _ = alert
    inc = incidents.open_incident(session, "boss", _t(), alert_id=a.id)
    lst = incidents.listing(session, "open")
    assert lst["counts"] == {"open": 1} and lst["unassessed"] == 1
    assert lst["rows"][0]["id"] == inc.id and lst["rows"][0]["reportable"] is None
    assert incidents.listing(session, "closed")["rows"] == []
    with pytest.raises(incidents.IncidentError):
        incidents.listing(session, "weird")
    d = incidents.detail(session, inc.id)
    assert d["log"][0]["kind"] == "opened" and incidents.detail(session, 999) is None
