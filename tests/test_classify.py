"""Exam classification: rules, projections, crosswalk to DRL codes, human overrides."""

import datetime as dt

import pytest
from sqlalchemy import select

from dozimetrix import classify
from dozimetrix.classify import default_rules, Rules, normalise, view_code
from dozimetrix.db.models import (
    Classification,
    ClassificationStatus,
    Code,
    CodeSystem,
    Event,
    Study,
)
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 3, 2, 10, 0)


@pytest.mark.parametrize("text,family,code", [
    ("W035 Torace a.p. 10kg-15kg", "rx", "RG-TH"),
    ("X035 Pulmonara 0-5 kg", "rx", "RG-TH"),
    ("W008 Sinusuri 11-15 Years", "rx", "RG-CRSN"),
    ("W001a Craniu lat. 0-5 Years", "rx", "RG-CR"),
    ("W002 Piramida Nazala lat", "rx", "RG-CRPN"),
    ("ANTEBRAT", "rx", "RG-MSAB"),
    ("Forearm bone", "rx", "RG-MSAB"),
    ("COLOANA_PASTING", "rx", "RG-CVOS"),
    ("Lumbar-spine", "rx", "RG-CVLM"),
    ("X077a Deget a.p.", "rx", "RG-MSDG"),
    ("Deget picior", "rx", "RG-MIDG"),
    ("COLOANA", "rx", None),  # which part of the spine is unknown: left for review
    ("Spine", "rx", None),
    ("Cistografie 5-15kg", "rf", "RF-PVCM"),
    ("Gastrointestinal Sup. 15-30kg", "rf", "RF-ABGD"),
    ("Head^CAP_SPIRAL_NATIV (Child)", "ct", "CT-CR"),
    ("Thorax^TAP_contrast (Adult)", "ct", "CT-TAP"),
    ("Abdomen^Abdomen_CONTRAST (Child)", "ct", "CT-AB"),
    ("Neck^REG_CERVICAL_CONTRAST (Child)", "ct", "CT-CERV"),
    ("Spine^SpineRoutine (Adult)", "ct", None),
])
def test_rules(text, family, code):
    assert Rules.load().match(family, normalise(text)) == code


def test_normalise_and_views():
    assert normalise("  Torace  a.p./Ş ") == " torace a p s "
    assert view_code("antero-posterior", None) == "AP"
    assert view_code("left lateral", None) == "LL"
    assert view_code("postero-anterior oblique", None) == "PA"
    assert view_code(None, "W035 Torace p.a. 30-50 Kg") == "PA"
    assert view_code(None, "W001a Craniu lat. 0-5 Years") == "LL"
    assert view_code(None, "ANTEBRAT") is None


def test_contrast_modes():
    r = Rules.load()
    assert r.contrast_mode([normalise("Head^CAP_SPIRAL_NATIV")]) == "N"
    assert r.contrast_mode([normalise("Abdomen_contrast")]) == "C"
    assert r.contrast_mode([normalise("cap nativ"), normalise("cap contrast")]) == "NC"
    assert r.contrast_mode([normalise("ficat trifazic")]) == "MF"
    # local abbreviations: SC / SDC (substanță de contrast), K (Kontrast), IV
    assert r.contrast_mode([normalise("Head^CAP_SPIRAL_NATIV"), normalise("TAP nativ"),
                            normalise("TAP SC"), normalise("CER K")]) == "NC"
    assert r.contrast_mode([normalise("ABD K PORT")]) == "C"
    assert r.contrast_mode([normalise("cerebral sdc")]) == "C"
    assert r.contrast_mode([normalise("CAP IV")]) == "C"
    # a children's age band, not contrast
    assert r.contrast_mode([normalise("CEREBRAL NATIV K 0-3 ani")]) == "N"


def test_header_contrast_agent_makes_the_series_post_contrast(session):
    load_all(session)
    st = Study(study_uid="c.hc", modality="CT", study_datetime=T, source_kind="test",
               study_description="Head^CAP_SPIRAL_NATIV (Adult)")
    st.events = [Event(protocol="Topogram", is_localizer=True), Event(protocol="TAP 1"),
                 Event(protocol="TAP 2", extra_json={"source": "header",
                                                     "contrast": {"agent": "IOMERON 350"}})]
    session.add(st)
    session.flush()
    classify.run(session, study_ids=[st.id])
    session.refresh(st)
    assert st.contrast_mode == "NC"  # "NATIV" in the text, an agent in a header


def _codes(session, study_id, event_id="any"):
    q = (select(CodeSystem.key, Code.code)
         .join(Code, Code.system_id == CodeSystem.id)
         .join(Classification, Classification.code_id == Code.id)
         .where(Classification.study_id == study_id))
    if event_id != "any":
        q = q.where(Classification.event_id.is_(event_id) if event_id is None
                    else Classification.event_id == event_id)
    return {f"{s}:{c}" for s, c in session.execute(q)}


def test_run_dx_per_exposure_ct_per_study_and_override(session):
    load_all(session)
    dx = Study(study_uid="c.1", modality="DX", study_datetime=T, source_kind="test",
               study_description="- - - - -")
    dx.events = [Event(protocol="W035 Torace a.p. 10kg-15kg", view="antero-posterior"),
                 Event(protocol="W035 Torace lat 10kg", view="lateral"),
                 Event(protocol="COLOANA", anatomy="Spine")]
    ct = Study(study_uid="c.2", modality="CT", study_datetime=T,
               study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test")
    ct.events = [Event(protocol="Topogram", is_localizer=True), Event(protocol="Head 5mm")]
    session.add_all([dx, ct])
    session.flush()

    stats = classify.run(session)
    assert stats["studies"] == 2
    ap, lat, spine = dx.events
    assert _codes(session, dx.id, ap.id) == {
        "ro-nom-rx:RG-TH", "ro-drl-ms838-2025:RX-TORACE-PA-AP", "ec-rp185:RX-THORAX-AP-PA",
        "ro-insp-1255-rx:T1"}
    assert _codes(session, dx.id, lat.id) == {"ro-nom-rx:RG-TH",
                                               "ro-drl-ms838-2025:RX-TORACE-LAT",
                                               "ro-insp-1255-rx:T2"}
    assert _codes(session, dx.id, spine.id) == set()
    assert _codes(session, dx.id, None) == {"ro-nom-rx:RG-TH"}
    assert _codes(session, ct.id) == {"ro-nom-ct:CT-CR", "ro-drl-ms838-2025:CT-CAP",
                                      "ec-rp185:CT-HEAD", "ro-insp-1255-ct:C"}
    session.refresh(ct)
    assert ct.contrast_mode == "N"

    # a person decides that this site's bare "COLOANA" protocol is the lumbar spine
    lumbar = session.scalar(select(Code).join(CodeSystem)
                            .where(CodeSystem.key == "ro-nom-rx", Code.code == "RG-CVLM"))
    classify.set_mapping(session, "rx", "protocol", "Coloana", lumbar)
    ids = classify.studies_with_text(session, "rx", "protocol", "coloana")
    assert ids == [dx.id]
    classify.run(session, ids)
    assert "ro-nom-rx:RG-CVLM" in _codes(session, dx.id, spine.id)
    assert _codes(session, dx.id, None) == {"ro-nom-rx:RG-TH", "ro-nom-rx:RG-CVLM"}
    row = session.scalar(select(Classification).where(Classification.event_id == spine.id,
                                                      Classification.mapping_id.is_not(None)))
    assert row.status == ClassificationStatus.confirmed

    # re-running is stable (rows replaced, not duplicated)
    before = len(_codes(session, dx.id))
    classify.run(session)
    assert len(_codes(session, dx.id)) == before


def test_national_ct_code_follows_contrast(session):
    load_all(session)
    s = Study(study_uid="c.9", modality="CT", study_datetime=T, source_kind="test",
              study_description="Abdomen^Abdomen_contrast (Adult)")
    session.add(s)
    session.flush()
    classify.run(session, [s.id])
    assert "ro-insp-1255-ct:ASC" in _codes(session, s.id)


def test_ct_series_of_another_region_get_their_own_codes(session):
    load_all(session)
    s = Study(study_uid="c.10", modality="CT", study_datetime=T, source_kind="test",
              study_description="Head^CAP_SPIRAL_NATIV (Adult)")
    head, tap, unknown = (Event(protocol="CAP NATIV"), Event(protocol="TAP nativ"),
                          Event(protocol="ARTERIAL"))
    s.events = [head, tap, unknown]
    session.add(s)
    session.flush()
    classify.run(session, [s.id])
    assert _codes(session, s.id, tap.id) == {"ro-nom-ct:CT-TAP",
                                             "ro-drl-ms838-2025:CT-TRUNCHI"}
    assert _codes(session, s.id, head.id) == set()  # same region: follows the study
    assert _codes(session, s.id, unknown.id) == set()  # names no region: follows the study
    assert "ro-drl-ms838-2025:CT-CAP" in _codes(session, s.id, None)


def test_chest_without_projection_is_frontal_and_pr_is_lateral(session):
    load_all(session)
    s = Study(study_uid="c.11", modality="DX", study_datetime=T, source_kind="test",
              study_description="- - - - -")
    front = Event(protocol="X035 Pulmonara 0-5 kg")
    side = Event(protocol="X035 Pulmonara pr. 0-5 kg")
    s.events = [front, side]
    session.add(s)
    session.flush()
    classify.run(session, [s.id])
    assert "ro-insp-1255-rx:T1" in _codes(session, s.id, front.id)
    assert "ro-insp-1255-rx:T2" in _codes(session, s.id, side.id)


# ---------------------------------------------------------------- MG, NM, PET


@pytest.mark.parametrize("text,family,code", [
    (" pet ct fdg fluorodeoxyglucose f 18 ", "nmh", "PCT1"),
    (" pet ct psma ga 68 ", "nmh", "PCT3"),
    (" pet ct prostata psma 1007 f 18 ", "nmh", "PCT2"),
    (" pet ct hepatic fdg f 18 ", "nmh", "PCT4"),
    (" wb bone tc99m mdp ", "nmh", "SCTO"),
    (" pet ct ", "nmh", None),  # agent unknown: left for review
    (" scintigrafie osoasa tc99m mdp tc 99m ", "nm", "SO2"),
    (" scintigrafie osoasa hdp tc 99m ", "nm", "SO3"),
    (" wb trifazica ", "nm", None),
    (" scintigrafie tiroidiana pertechnetat tc 99m ", "nm", "ST1"),
    (" captare tiroidiana i 131 ", "nm", "ST3"),
    (" scintigrafie paratiroidiana sestamibi tc 99m ", "nm", "SPT3"),
    (" scintigrafie renala dmsa tc 99m ", "nm", "SR4"),
    (" limfoscintigrafie nanocoloid tc 99m ", "nm", "SL1"),
])
def test_nm_rules(text, family, code):
    assert Rules.load().match(family, text) == code


def test_nm_text_adds_the_implied_radionuclide():
    text = classify.nm_text({"study_description": "PET-CT"},
                            [{"event_type": "radiopharmaceutical",
                              "protocol": "FDG -- fluorodeoxyglucose", "comment": None}])
    assert " f 18 " in text and " fdg " in text


def test_mammography_views():
    assert classify.mg_view("cranio-caudal", None) == "CC"
    assert classify.mg_view("medio-lateral oblique", None) == "MLO"
    assert classify.mg_view(None, "L MLO") == "MLO"
    assert classify.mg_view("latero-medial", None) is None


def test_run_mammography_nm_and_pet(session):
    load_all(session)
    bi = Study(study_uid="m.1", modality="MG", study_datetime=T, source_kind="test")
    bi.events = [Event(view="cranio-caudal", laterality="R", agd=1.1),
                 Event(view="MLO", laterality="R", agd=1.3),
                 Event(view="cranio-caudal", laterality="L", agd=1.0),
                 Event(protocol="L MLO", laterality="L", agd=1.2)]
    uni = Study(study_uid="m.2", modality="MG", study_datetime=T, source_kind="test")
    uni.events = [Event(view="cranio-caudal", laterality="L", agd=1.0)]
    dbt = Study(study_uid="m.3", modality="MG", study_datetime=T, source_kind="test",
                study_description="Tomosynthesis bilateral")
    dbt.events = [Event(view="CC", laterality="R", agd=2.0),
                  Event(view="CC", laterality="L", agd=2.0)]
    pet = Study(study_uid="n.1", modality="PT", study_datetime=T, source_kind="test",
                study_description="PET-CT")
    pet.events = [Event(event_type="radiopharmaceutical", protocol="FDG -- fluorodeoxyglucose")]
    spect = Study(study_uid="n.2", modality="NM", study_datetime=T, source_kind="test",
                  study_description="WB Bone")
    spect.events = [Event(event_type="radiopharmaceutical", protocol="Tc99m MDP"),
                    Event(event_type="spiral", dlp=120.0)]
    bone = Study(study_uid="n.3", modality="NM", study_datetime=T, source_kind="test",
                 study_description="Scintigrafie osoasa")
    bone.events = [Event(event_type="radiopharmaceutical", protocol="HDP")]
    unknown = Study(study_uid="n.4", modality="NM", study_datetime=T, source_kind="test",
                    study_description="WB TRIFAZICA")
    unknown.events = [Event(event_type="radiopharmaceutical", comment="Tc-99m")]
    session.add_all([bi, uni, dbt, pet, spect, bone, unknown])
    session.flush()
    classify.run(session)

    rcc, rmlo, lcc, lmlo = bi.events
    assert _codes(session, bi.id, rcc.id) == {"ro-insp-1255-rx:SMB1",
                                               "ro-drl-ms838-2025:MG-UNILATERAL"}
    assert _codes(session, bi.id, lmlo.id) == {"ro-insp-1255-rx:SMB2",
                                                "ro-drl-ms838-2025:MG-UNILATERAL"}
    assert _codes(session, bi.id, None) == {"ro-insp-1255-rx:SMB1", "ro-insp-1255-rx:SMB2"}
    assert _codes(session, uni.id) == {"ro-insp-1255-rx:SMU1",
                                       "ro-drl-ms838-2025:MG-UNILATERAL"}
    assert "ro-drl-ms838-2025:MG-DBT-BILATERAL" in _codes(session, dbt.id)
    assert _codes(session, pet.id) == {"ro-insp-1255-nm-hybrid:PCT1",
                                       "ro-drl-ms838-2025:PETCT-FDG"}
    assert _codes(session, spect.id) == {"ro-insp-1255-nm-hybrid:SCTO"}
    assert _codes(session, bone.id) == {"ro-insp-1255-nm:SO3",
                                        "ro-drl-ms838-2025:NM-OSOS-HDP"}
    assert _codes(session, unknown.id) == set()

    # a person maps this site's "WB TRIFAZICA" (no agent in the header)
    so2 = session.scalar(select(Code).join(CodeSystem)
                         .where(CodeSystem.key == "ro-insp-1255-nm", Code.code == "SO2"))
    classify.set_mapping(session, "nm", "study_description", "WB TRIFAZICA", so2)
    ids = classify.studies_with_text(session, "nm", "study_description", "wb trifazica")
    assert ids == [unknown.id]
    classify.run(session, ids)
    assert _codes(session, unknown.id) == {"ro-insp-1255-nm:SO2"}


def test_generic_limb_ct():
    from dozimetrix import classify

    rules = classify.default_rules()
    assert rules.match("ct", classify.normalise("Lower Extremities^ExtrCombi (Adult)")) == "CT-MBI"
    assert rules.match("ct", classify.normalise("Upper Extremities^MEMBRU_SUP (Child)")) == "CT-MBS"
    knee = classify.normalise("Lower Extremities^Knee")
    assert rules.match("ct", knee) == "CT-MIGN"  # a specific segment wins
