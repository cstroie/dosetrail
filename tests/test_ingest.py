"""DICOM parsing and idempotent storage on synthetic objects."""

import hashlib

import pytest
from sqlalchemy import func, select

from dosimetrix.db.models import Device, Event, IngestedObject, Patient, Study
from dosimetrix.ingest.identity import device_key
from dosimetrix.ingest.parse import parse
from dosimetrix.ingest.store import Store
from tests import dicom_factory as f

SECRET = b"t" * 32
MG_PRES = "1.2.840.10008.5.1.4.1.1.1.2"


# ------------------------------------------------------------------------------ parsing


def test_ct_rdsr_values_and_units():
    p = parse(f.ct_rdsr(dlp_unit="mGy*cm"))  # non-UCUM unit seen on a real scanner
    assert p.kind == "ct_rdsr" and p.study["modality"] == "CT"
    loc, spiral = p.events
    assert loc["is_localizer"] and loc["event_type"] == "localizer"
    assert spiral["event_type"] == "spiral" and spiral["dlp"] == 820.0
    assert spiral["ct_phantom_cm"] == 16 and spiral["scan_length_mm"] == 180.0  # cm -> mm
    assert spiral["kvp"] == 120 and spiral["ma"] == 200 and spiral["pitch"] == 0.9
    assert not spiral["dose_check_alert"]
    assert p.device["device_observer_uid"] == "9.9.9"


def test_dose_check_alert_only_when_raised():
    assert parse(f.ct_rdsr(alert_raised=True)).events[1]["dose_check_alert"]


def test_dose_report_inside_secondary_capture_is_recognised():
    p = parse(f.ct_rdsr(sop_class=f.SC))
    assert p.kind == "ct_rdsr" and len(p.events) == 2


def test_projection_rdsr():
    p = parse(f.proj_rdsr())
    fl, acq = p.events
    assert p.kind == "proj_rdsr" and p.study["modality"] == "RF"
    assert fl["event_type"] == "fluoro" and fl["fluoro_time_s"] == 30
    assert fl["dap"] == pytest.approx(0.2)  # 2 dGy·cm² -> 0.2 Gy·cm²
    assert fl["rp_dose"] == pytest.approx(0.005)  # 5 mGy -> Gy
    assert fl["filter_cu_mm"] == 0.1 and fl["plane"] == "A" and acq["plane"] == "B"


def test_dx_header():
    p = parse(f.dx_image(manufacturer="KODAK", model="DR 7500"))
    e = p.events[0]
    assert p.kind == "header" and e["event_uid"] == "1.2.3.300.1"
    assert e["dap"] == pytest.approx(0.05)  # 0.5 dGy·cm² -> Gy·cm²
    assert e["mas"] == 2.5 and e["exposure_time_s"] == 0.01
    assert e["sid_mm"] == 1800  # Kodak DR 7500 reports decimetres
    assert e["filter_cu_mm"] == 0.1 and e["filter_al_mm"] == 1.0
    assert e["ei_vendor_unit"] == "EI (mbels)" and e["exposure_index"] == 300


def test_text_report_ignored():
    assert parse(f.text_report()).kind == "ignored"


# ------------------------------------------------------------------------------ storing


def _events(session, uid):
    st = session.scalar(select(Study).filter_by(study_uid=uid))
    return st, list(session.scalars(select(Event).filter_by(study_id=st.id)))


def test_store_is_idempotent(session):
    store = Store(session, SECRET)
    ds = f.ct_rdsr()
    assert store.store(parse(ds)).status == "imported"
    assert store.store(parse(ds)).status == "duplicate"
    st, evs = _events(session, "1.2.3.100")
    assert len(evs) == 2 and st.total_dlp == pytest.approx(822.5)
    assert st.n_localizer == 1 and st.n_spiral == 1 and st.ctdivol_max == 45.0
    assert st.age_precision == "exact" and st.patient.pseudo_id.startswith("P-")


def test_cumulative_rdsr_adds_only_new_events(session):
    store = Store(session, SECRET)
    first = [("e1", "113805", "Constant Angle Acquisition", "Topogram", 0.1, 2.5)]
    both = first + [("e2", "P5-08001", "Spiral Acquisition", "Head", 45.0, 820.0)]
    store.store(parse(f.ct_rdsr(study_uid="1.2.3.110", sop_uid="1.2.3.110.1", events=first)))
    r = store.store(parse(f.ct_rdsr(study_uid="1.2.3.110", sop_uid="1.2.3.110.2",
                                    events=both)))
    assert r.events_added == 1
    st, evs = _events(session, "1.2.3.110")
    assert sorted(e.event_uid for e in evs) == ["e1", "e2"] and st.n_events == 2


def test_rdsr_supersedes_image_headers(session):
    store = Store(session, SECRET)
    uid = "1.2.3.200"
    store.store(parse(f.dx_image(study_uid=uid, sop_uid=uid + ".9")))
    store.store(parse(f.proj_rdsr(study_uid=uid, sop_uid=uid + ".1")))
    _, evs = _events(session, uid)
    assert sorted(e.event_uid for e in evs) == ["a1", "f1"]  # header event replaced
    r = store.store(parse(f.dx_image(study_uid=uid, sop_uid=uid + ".8")))
    assert r.status == "superseded"


def test_for_processing_copy_is_not_counted_twice(session):
    store = Store(session, SECRET)
    store.store(parse(f.dx_image(study_uid="1.2.3.310", sop_uid="1.2.3.310.1")))
    store.store(parse(f.dx_image(study_uid="1.2.3.310", sop_uid="1.2.3.310.2",
                                 sop_class=f.DX_PROC)))
    st, evs = _events(session, "1.2.3.310")
    assert len(evs) == 1 and st.total_dap == pytest.approx(0.05)
    assert store.stats["duplicate_exposures"] == 1


def test_links_to_existing_patient_and_device(session):
    legacy = Patient(legacy_sha256=hashlib.sha256(b"P1").hexdigest(), pseudo_id="P-LEGAC-Y0001")
    dev = Device(identity_key=device_key("ACME", "Model X", "DX1", "DXROOM"),
                 manufacturer="ACME", display_name="DR Room 1", is_local=True)
    session.add_all([legacy, dev])
    session.flush()
    store = Store(session, SECRET)
    r = store.store(parse(f.dx_image(study_uid="1.2.3.320", sop_uid="1.2.3.320.1")))
    st = session.get(Study, r.study_id)
    assert st.patient_id == legacy.id and legacy.patient_id == "P1"  # hash -> clear link
    assert st.device_id == dev.id
    # an unknown device is created as external until an admin decides
    r2 = store.store(parse(f.dx_image(study_uid="1.2.3.321", sop_uid="1.2.3.321.1",
                                      serial="NEW")))
    assert session.get(Study, r2.study_id).device.is_local is False


def test_ignored_objects_are_recorded_once(session):
    store = Store(session, SECRET)
    assert store.store(parse(f.text_report())).status == "ignored"
    assert store.store(parse(f.text_report())).status == "duplicate"
    n = session.scalar(select(func.count()).select_from(IngestedObject)
                       .where(IngestedObject.status == "ignored"))
    assert n == 1


def test_glued_vendor_dose_units():
    from dosimetrix.dicom.units import convert, normalise_unit

    # Siemens Ysio writes the DAP unit as 'Gym2' (UCUM: Gy.m2)
    assert normalise_unit("Gym2") == "gy.m2" and normalise_unit("mGycm") == "mgy.cm"
    assert convert(0.00000129, "Gym2", "Gy.cm2") == pytest.approx(0.0129)
    assert normalise_unit("mGy") == "mgy" and normalise_unit("Gy") == "gy"


def test_measurement_without_a_number():
    from pydicom.dataset import Dataset

    from dosimetrix.dicom.sr import tree

    item = Dataset()
    item.ValueType, item.RelationshipType = "NUM", "CONTAINS"
    cn = Dataset()
    cn.CodeValue, cn.CodingSchemeDesignator, cn.CodeMeaning = "113738", "DCM", "Dose (RP)"
    item.ConceptNameCodeSequence = [cn]
    mv = Dataset()  # no NumericValue: a Numeric Value Qualifier explains why
    u = Dataset()
    u.CodeValue, u.CodingSchemeDesignator, u.CodeMeaning = "Gy", "UCUM", "Gy"
    mv.MeasurementUnitsCodeSequence = [u]
    item.MeasuredValueSequence = [mv]
    root = Dataset()
    root.ValueType, root.ContentSequence = "CONTAINER", [item]
    root.ConceptNameCodeSequence = [cn]
    assert tree(root).num("113738", "Gy") is None


# ------------------------------------------------------------------- staff, NM / PET


def test_operator_from_the_dose_report_participants():
    p = parse(f.ct_rdsr(staff=[("TECH^ANA", "113851"), ("DOC^ION", "113850")]))
    assert p.study["operator_name"] == "TECH^ANA"
    assert p.study["performing_physician"] == "DOC^ION"
    assert parse(f.ct_rdsr()).study["operator_name"] is None


def test_ct_image_header_fills_the_staff_of_a_known_study(session):
    store = Store(session, SECRET)
    assert store.store(parse(f.ct_image(study_uid="9.9.1", sop_uid="9.9.1.1"))).status \
        == "ignored"  # a study we do not have
    store.store(parse(f.ct_rdsr()))
    r = store.store(parse(f.ct_image()))
    assert r.status == "imported"
    s = session.scalar(select(Study).filter_by(study_uid="1.2.3.100"))
    assert (s.operator_name, s.performing_physician) == ("RAD^ANA", "DOC^ION")
    assert s.n_events == 2  # no events from an image header
    # names already there are kept
    store.store(parse(f.ct_image(sop_uid="1.2.3.100.8", operator="OTHER^ONE")))
    assert s.operator_name == "RAD^ANA"
    assert parse(f.ct_image(operator=None, physician=None)).kind == "ignored"


def test_pet_header_administered_activity(session):
    p = parse(f.pet_image())
    assert p.kind == "nm_header" and p.study["modality"] == "PT"
    (ev,) = p.events
    assert ev["protocol"] == "Fluorodeoxyglucose" and ev["comment"] == "^18^Fluorine"
    assert ev["extra_json"]["activity_mbq"] == pytest.approx(185.0)
    store = Store(session, SECRET)
    store.store(p)
    store.store(parse(f.pet_image(sop_uid="1.2.3.500.2")))  # another image, same injection
    s = session.scalar(select(Study).filter_by(study_uid="1.2.3.500"))
    assert s.n_events == 1 and s.admin_activity_mbq == pytest.approx(185.0)


def test_nm_header_with_only_the_radionuclide():
    ds = f.pet_image(study_uid="1.2.3.600", sop_uid="1.2.3.600.1")
    rp = ds.RadiopharmaceuticalInformationSequence[0]
    del rp.Radiopharmaceutical, rp.RadionuclideTotalDose, rp.RadiopharmaceuticalStartDateTime
    (ev,) = parse(ds).events  # Mediso NM: Tc-99m, no agent, no activity
    assert ev["comment"] == "^18^Fluorine" and ev["extra_json"]["activity_mbq"] is None
    del rp.RadionuclideCodeSequence
    assert parse(ds).kind == "ignored"


def test_mammography_header_organ_dose_is_in_dgy():
    ds = f.dx_image(study_uid="1.2.3.700", sop_uid="1.2.3.700.1", sop_class=MG_PRES)
    ds.Modality = "MG"
    ds.OrganDose = "0.0123"  # dGy
    ds.BodyPartThickness = "52"
    (ev,) = parse(ds).events
    assert ev["agd"] == pytest.approx(1.23) and ev["compression_thickness_mm"] == 52
