"""Data-quality rules."""

import datetime as dt

from dosetrail import classify, quality
from dosetrail.db.models import Device, Event, Patient, SizeSource, Study
from dosetrail.seeds.loader import load_all

T = dt.datetime(2026, 9, 1, 10)


def _rules(findings):
    return sorted(f["rule"] if isinstance(f, dict) else f.rule for f in findings)


def test_weight_bounds():
    assert quality.weight_bounds(100) == (0.3, 15.0)
    assert quality.weight_bounds(int(30 * 365.25)) == (25.0, 250.0)


def test_rules_on_studies(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="q-1", is_local=True)
    kid = Patient(pseudo_id="P-Q1", sex="F")
    nobody = Patient(pseudo_id="P-Q2", sex=None)
    session.add_all([dev, kid, nobody])
    session.flush()
    good = Study(study_uid="q.1", modality="CT", device_id=dev.id, patient_id=kid.id,
                 study_datetime=T, age_days=2000, weight_kg=18.0,
                 weight_source=SizeSource.measured, source_kind="test",
                 study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
    good.events = [Event(protocol="Head", ctdivol=30.0, scan_length_mm=150.0, dlp=450.0,
                         ct_phantom_cm=16)]
    bad = Study(study_uid="q.2", modality="CT", device_id=dev.id, patient_id=kid.id,
                study_datetime=T + dt.timedelta(seconds=40), age_days=2000, weight_kg=95.0,
                source_kind="test", study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
    bad.events = [Event(protocol="Head", ctdivol=30.0, scan_length_mm=150.0, dlp=900.0,
                        ct_phantom_cm=32),
                  Event(protocol="TAP nativ", ctdivol=10.0, scan_length_mm=400.0, dlp=400.0,
                        ct_phantom_cm=32),  # a body series at 32 cm is fine
                  Event(protocol="Head 2", ctdivol=400.0, scan_length_mm=100.0, dlp=4000.0,
                        ct_phantom_cm=16)]
    anon = Study(study_uid="q.3", modality="DX", device_id=dev.id, patient_id=nobody.id,
                 study_datetime=T, source_kind="test", study_description="Chest")
    session.add_all([good, bad, anon])
    session.flush()
    classify.run(session)

    assert quality.study_findings(session, good.id) == [
        {"rule": "possible_duplicate", "severity": "warn",
         "title": quality.RULES["possible_duplicate"][1], "detail": f"study {bad.id}"}]
    found = quality.study_findings(session, bad.id)
    assert _rules(found) == ["ct_dlp_mismatch", "dose_absurd", "dose_far_above_drl",
                             "phantom_32_head", "possible_duplicate", "weight_implausible"]
    assert next(f for f in found if f["rule"] == "phantom_32_head")["detail"] == "Head"
    assert next(f for f in found if f["rule"] == "dose_absurd")["severity"] == "error"
    assert _rules(quality.study_findings(session, anon.id)) == ["age_missing", "sex_missing"]

    out = quality.run(session, [])
    assert out["studies"] == 3 and out["rules"]["phantom_32_head"]["count"] == 1
    (d,) = out["devices"]
    assert d["share"]["weight_measured"] == round(1 / 3, 4) and d["flagged"] == 2
    assert {f["id"] for f in out["flagged"]} == {good.id, bad.id}  # missing data alone is
    # counted but does not put a study on the flagged list


def test_zero_dap_is_missing(session):
    from dosetrail import maintenance
    from dosetrail.ingest.store import update_study_totals, zero_as_missing

    ev = zero_as_missing({"dap": 0.0, "rp_dose": 0.0, "sid_mm": 0.0, "kvp": 72.0,
                          "extra_json": {"source": "rdsr"}})
    assert ev["dap"] is None and ev["sid_mm"] is None and ev["kvp"] == 72.0
    assert ev["extra_json"] == {"source": "rdsr", "reported_zero": ["dap", "rp_dose", "sid_mm"]}
    assert zero_as_missing({"dap": 0.01}) == {"dap": 0.01}

    load_all(session)
    dev = Device(display_name="Mobile", identity_key="q-z", is_local=True)
    kid = Patient(pseudo_id="P-Z1", sex="M")
    session.add_all([dev, kid])
    session.flush()
    s = Study(study_uid="z.1", modality="DX", device_id=dev.id, patient_id=kid.id,
              study_datetime=T, age_days=30, source_kind="test", study_description="Chest")
    s.events = [Event(dap=0.0, kvp=72.0), Event(dap=0.0, kvp=72.0)]
    session.add(s)
    session.flush()
    update_study_totals(session, s)
    assert s.total_dap == 0.0
    assert maintenance.zero_doses_as_missing(session) == 1
    session.refresh(s)
    assert s.total_dap is None and all(e.dap is None for e in s.events)
    assert "dap_missing" in _rules(quality.study_findings(session, s.id))


def test_replaced_weight_is_reported(session):
    load_all(session)
    dev = Device(display_name="CT W", identity_key="q-w", is_local=True)
    kid = Patient(pseudo_id="P-W1", sex="M")
    session.add_all([dev, kid])
    session.flush()
    s = Study(study_uid="w.1", modality="CT", device_id=dev.id, patient_id=kid.id,
              study_datetime=T, age_days=2000, weight_kg=18.4,
              weight_source=SizeSource.estimated_reference, weight_reported_kg=185.0,
              source_kind="test", study_description="Head")
    session.add(s)
    session.flush()
    (f,) = [x for x in quality.study_findings(session, s.id) if x["rule"] == "weight_implausible"]
    assert "reported 185 kg" in f["detail"] and "estimate 18.4 kg" in f["detail"]
