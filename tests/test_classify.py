"""Exam classification: rules, projections, crosswalk to DRL codes, human overrides."""

import datetime as dt

import pytest
from sqlalchemy import select

from dosetrail import classify
from dosetrail.classify import Rules, normalise, view_code
from dosetrail.db.models import Classification, ClassificationStatus, Code, CodeSystem, Event, Study
from dosetrail.seeds.loader import load_all

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
