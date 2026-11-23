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


def test_mammogram_copies_are_not_counted_twice(session):
    store = Store(session, SECRET)
    for sop, cls in (("1.2.3.710.1", MG_PRES), ("1.2.3.710.2", "1.2.840.10008.5.1.4.1.1.1.2.1")):
        ds = f.dx_image(study_uid="1.2.3.710", sop_uid=sop, sop_class=cls)
        ds.Modality, ds.OrganDose, ds.ImageLaterality = "MG", "0.0125", "L"
        del ds.ImageAndFluoroscopyAreaDoseProduct
        store.store(parse(ds))
    s = session.scalar(select(Study).filter_by(study_uid="1.2.3.710"))
    assert s.n_events == 1 and s.agd_left == pytest.approx(1.25)


def test_mammography_laterality_as_a_target_region_modifier():
    ds = f.proj_rdsr(study_uid="1.2.3.720", sop_uid="1.2.3.720.1")
    ev = next(it for it in ds.ContentSequence
              if it.ConceptNameCodeSequence[0].CodeValue == "113706")
    ev.ContentSequence.append(f.item("CODE", ("123014", "Target Region"),
                                     ("76752008", "Breast", "SCT"), children=[
        f.item("CODE", ("272741003", "Laterality"), ("24028007", "Right", "SCT"),
               rel="HAS CONCEPT MOD")]))
    assert parse(ds).events[0]["laterality"] == "R"


def test_pet_ct_is_pt_whichever_part_comes_first(session):
    store = Store(session, SECRET)
    store.store(parse(f.ct_rdsr(study_uid="1.2.3.510", sop_uid="1.2.3.510.1")))
    store.store(parse(f.pet_image(study_uid="1.2.3.510", sop_uid="1.2.3.510.2")))
    s = session.scalar(select(Study).filter_by(study_uid="1.2.3.510"))
    assert s.modality == "PT" and s.total_dlp and s.admin_activity_mbq == pytest.approx(185.0)
    store.store(parse(f.pet_image(study_uid="1.2.3.511", sop_uid="1.2.3.511.1")))
    store.store(parse(f.ct_rdsr(study_uid="1.2.3.511", sop_uid="1.2.3.511.2")))
    t = session.scalar(select(Study).filter_by(study_uid="1.2.3.511"))
    assert t.modality == "PT" and t.total_dlp
    from dosimetrix.hybrid import label

    assert label(s.modality, s.total_dlp) == "PET/CT" and label("PT", None) == "PT"


def test_recompute_relabels_pet_ct_filed_as_ct(session):
    from dosimetrix.maintenance import relabel_hybrid

    store = Store(session, SECRET)
    store.store(parse(f.ct_rdsr(study_uid="1.2.3.520", sop_uid="1.2.3.520.1")))
    store.store(parse(f.pet_image(study_uid="1.2.3.520", sop_uid="1.2.3.520.2")))
    s = session.scalar(select(Study).filter_by(study_uid="1.2.3.520"))
    s.modality = "CT"  # as the code before this change stored it
    session.flush()
    assert relabel_hybrid(session) == 1
    session.expire_all()
    assert session.get(Study, s.id).modality == "PT"
    assert relabel_hybrid(session) == 0


def test_one_administration_in_two_studies_counts_once(session):
    """An early and a delayed PET sent as two studies: the injection counts on the first."""
    store = Store(session, SECRET)
    late = f.pet_image(study_uid="1.2.3.530", sop_uid="1.2.3.530.1", mbq=126.06)
    late.StudyTime = "120000"
    early = f.pet_image(study_uid="1.2.3.531", sop_uid="1.2.3.531.1", mbq=126.06)
    early.StudyTime = "102200"
    store.store(parse(late))  # the later study arrives first
    store.store(parse(early))
    a = session.scalar(select(Study).filter_by(study_uid="1.2.3.531"))
    b = session.scalar(select(Study).filter_by(study_uid="1.2.3.530"))
    assert a.admin_activity_mbq == pytest.approx(126.06)
    assert b.admin_activity_mbq is None
    (ev,) = b.events
    assert ev.extra_json["same_administration_as"] == a.id
    assert "same_administration_as" not in (a.events[0].extra_json or {})

    # another injection (stress after rest): its own administration
    other = f.pet_image(study_uid="1.2.3.532", sop_uid="1.2.3.532.1", mbq=400.0)
    store.store(parse(other))
    c = session.scalar(select(Study).filter_by(study_uid="1.2.3.532"))
    assert c.admin_activity_mbq == pytest.approx(400.0)

    from dosimetrix.maintenance import settle_administrations
    from dosimetrix.net.qr import needs_activity

    assert settle_administrations(session) == 0  # stable
    assert not needs_activity(session, "1.2.3.530")  # not fetched again


def test_lookup_tables_are_built_once_per_store(session, monkeypatch):
    """Classifier / Matcher / Evaluator load whole tables: not once per object."""
    from dosimetrix import alerts, classify, indications

    built = {"c": 0, "m": 0, "e": 0}
    for mod, name, key in ((classify, "Classifier", "c"), (indications, "Matcher", "m"),
                           (alerts, "Evaluator", "e")):
        orig = getattr(mod, name)

        def make(*a, _o=orig, _k=key, **kw):
            built[_k] += 1
            return _o(*a, **kw)
        monkeypatch.setattr(mod, name, make)
    store = Store(session, SECRET)
    for i in range(3):
        r = store.store(parse(f.ct_rdsr(study_uid=f"1.2.3.9{i}", sop_uid=f"1.2.3.9{i}.1")))
        assert r.status == "imported"
    assert built["c"] == 1 and built["e"] == 1 and built["m"] <= 1


# ------------------------------------------------------------ CT without a dose report


def test_ct_series_header_gives_an_estimated_event():
    p = parse(f.ct_series_image(ctdivol=10.0, count=50, spacing=5.0))
    assert p.kind == "ct_header" and p.study["modality"] == "CT"
    (e,) = p.events
    assert e["event_uid"] == "1.2.3.600.9" and e["event_type"] == "spiral"
    assert e["ctdivol"] == 10.0 and e["scan_length_mm"] == 250.0
    assert e["dlp"] == pytest.approx(250.0)  # 10 mGy x 25 cm
    assert e["extra_json"]["dlp_estimated"] and e["kvp"] == 120 and e["exposure_time_s"] == 0.5
    # without the image count (C-MOVE): the CTDIvol only, no DLP
    e2 = parse(f.ct_series_image(count=None)).events[0]
    assert e2["ctdivol"] == 10.0 and e2["dlp"] is None
    assert parse(f.ct_series_image(image_type=("DERIVED", "SECONDARY"))).kind == "ignored"
    loc = parse(f.ct_series_image(image_type=("ORIGINAL", "PRIMARY", "LOCALIZER"))).events[0]
    assert loc["is_localizer"] and loc["event_type"] == "localizer"


def test_ct_headers_import_then_the_dose_report_replaces_them(session):
    store = Store(session, SECRET)
    assert store.store(parse(f.ct_series_image(sop_uid="1.2.3.600.1"))).status == "imported"
    store.store(parse(f.ct_series_image(sop_uid="1.2.3.600.2", series_uid="1.2.3.600.8",
                                        ctdivol=4.0, count=20, spacing=3.0)))
    st = session.scalar(select(Study).filter_by(study_uid="1.2.3.600"))
    assert st.source_kind == "ct_header" and st.n_events == 2 and st.modality == "CT"
    assert st.total_dlp == pytest.approx(250.0 + 24.0)
    # the dose report arrives later: it replaces the estimated header events
    store.store(parse(f.ct_rdsr(study_uid="1.2.3.600", sop_uid="1.2.3.600.50")))
    session.refresh(st)
    assert st.source_kind == "ct_rdsr" and st.n_events == 2
    assert st.total_dlp == pytest.approx(822.5)
    # and a header arriving after the report is superseded
    assert store.store(parse(f.ct_series_image(sop_uid="1.2.3.600.3", series_uid="1.2.3.600.7"))
                       ).status == "superseded"
