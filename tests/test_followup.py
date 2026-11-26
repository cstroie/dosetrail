"""High-dose follow-up: creation from the dose levels, status flow, the patient letter."""

import datetime as dt

import pytest

from dozimetrix import followup
from dozimetrix.db.models import Device, Followup, Patient, Study

T = dt.datetime(2026, 11, 2, 10, 0)
TODAY = dt.date(2026, 11, 26)


@pytest.fixture
def world(session):
    dev = Device(display_name="Angio", identity_key="fu-1", is_local=True,
                 institution_name="Spital Test")
    pat = Patient(pseudo_id="P-FU1", sex="M", patient_id="ID-1", name="POPESCU^ION")
    session.add_all([dev, pat])
    session.flush()

    def study(uid, **kw):
        s = Study(study_uid=uid, device_id=dev.id, patient_id=pat.id, study_datetime=T,
                  source_kind="test", modality=kw.pop("modality", "XA"),
                  procedure_name="Coronarografie", **kw)
        session.add(s)
        session.flush()
        return s

    return study


def test_sync_creates_one_per_study_passing_a_level(session, world):
    psd = world("fu.1", psd_mgy=3400.0)
    kar = world("fu.2", total_rp_dose=5.2, total_dap=120.0)
    kap = world("fu.3", total_dap=610.0)
    low = world("fu.4", psd_mgy=1000.0, total_rp_dose=1.0, total_dap=50.0)
    ct = world("fu.5", modality="CT", total_dap=900.0)  # not fluoroscopy
    stats = followup.sync(session, [s.id for s in (psd, kar, kap, low, ct)])
    assert stats["new"] == 3
    rows = {f.study_id: f for f in session.query(Followup)}
    assert set(rows) == {psd.id, kar.id, kap.id}
    assert rows[psd.id].levels == "peak skin dose 3400 mGy ≥ 3000"
    assert rows[kar.id].levels == "Ka,r 5200 mGy ≥ 5000"
    assert rows[psd.id].check_due == dt.date(2026, 11, 16) and rows[psd.id].status == "open"
    # idempotent, and an open record follows a revised estimate
    psd.psd_mgy = 3900.0
    session.flush()
    assert followup.sync(session)["new"] == 0  # all studies: the same three
    assert rows[psd.id].levels == "peak skin dose 3900 mGy ≥ 3000"


def test_status_flow_and_listing(session, world):
    s = world("fu.1", psd_mgy=3400.0)
    followup.sync(session, [s.id])
    f = session.query(Followup).one()
    now = dt.datetime(2026, 11, 20, 9, 0)
    lst = followup.listing(session, "pending", TODAY)
    assert lst["counts"] == {"open": 1} and lst["overdue"] == 0  # 24 days: not yet 28
    with pytest.raises(followup.FollowupError, match="outcome"):
        followup.update(session, f.id, "boss", now, status="checked")
    with pytest.raises(followup.FollowupError, match="status"):
        followup.update(session, f.id, "boss", now, status="gone")
    followup.update(session, f.id, "boss", now, status="informed")
    assert (f.status, f.informed_by, f.informed_at) == ("informed", "boss", now)
    assert followup.listing(session, "pending", dt.date(2026, 12, 5))["overdue"] == 1
    followup.update(session, f.id, "boss", now, status="checked", outcome="no skin effect")
    assert f.checked_at == now and followup.listing(session, "pending", TODAY)["rows"] == []
    assert followup.listing(session, "checked", TODAY)["rows"][0]["outcome"] == "no skin effect"
    with pytest.raises(followup.FollowupError, match="not found"):
        followup.update(session, 999, "boss", now, status="open")


def test_dismissing_needs_a_reason(session, world):
    followup.sync(session, [world("fu.1", total_dap=700.0).id])
    f = session.query(Followup).one()
    with pytest.raises(followup.FollowupError, match="why"):
        followup.update(session, f.id, "boss", T, status="dismissed")
    followup.update(session, f.id, "boss", T, status="dismissed", note="phantom")
    assert f.status == "dismissed"


def test_letter_names_the_patient_in_both_languages(session, world):
    followup.sync(session, [world("fu.1", psd_mgy=3400.0).id])
    f = session.query(Followup).one()
    ro = followup.letter(session, f.id)
    assert "POPESCU ION" in ro and "Coronarografie" in ro and "16.11.2026" in ro
    assert "Spital Test" in ro and "peak skin dose 3400 mGy" in ro and "Stimate pacient" in ro
    assert "Dear patient" in followup.letter(session, f.id, "en")
    assert followup.letter(session, 999) is None
    with pytest.raises(followup.FollowupError):
        followup.letter(session, f.id, "xx")
