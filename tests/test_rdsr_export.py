"""RDSR export: stored study -> dose SR -> our parser reads back the same values."""

import io

import pydicom
import pytest

from dosimetrix.db.models import SizeSource, Study
from dosimetrix.export import rdsr
from dosimetrix.ingest.parse import parse
from dosimetrix.ingest.store import Store
from tests import dicom_factory as f

SECRET = b"r" * 32


def _store(session, ds):
    r = Store(session, SECRET).store(parse(ds))
    return session.get(Study, r.study_id)


def _ordered(st):
    import datetime as dt

    return sorted(st.events, key=lambda e: (e.event_datetime or dt.datetime.min, e.id))


def _roundtrip(ds):
    buf = io.BytesIO()
    rdsr.write(ds, buf)
    buf.seek(0)
    back = pydicom.dcmread(buf)
    assert back.file_meta.TransferSyntaxUID == pydicom.uid.ExplicitVRLittleEndian
    return back, parse(back)


FIELDS_CT = ("event_type", "protocol", "ctdivol", "dlp", "ct_phantom_cm", "scan_length_mm",
             "pitch", "kvp", "exposure_time_s")
FIELDS_PROJ = ("event_type", "plane", "protocol", "dap", "rp_dose", "kvp", "filter_cu_mm",
               "fluoro_time_s")


def test_ct_roundtrip_anonymised(session):
    st = _store(session, f.ct_rdsr(study_uid="5.1.1", sop_uid="5.1.1.1"))
    ds = rdsr.build(st, SECRET)
    back, p = _roundtrip(ds)
    assert p.kind == "ct_rdsr" and len(p.events) == len(st.events)
    for o, ev in zip(_ordered(st), p.events, strict=True):
        for k in FIELDS_CT:
            want = "localizer" if k == "event_type" and o.is_localizer else getattr(o, k)
            got = ev[k]
            assert got == pytest.approx(want) if isinstance(want, float) else got == want, k
    # anonymised: no clear identifiers, stable derived UIDs
    assert back.PatientID == st.patient.pseudo_id and back.PatientName == ""
    assert back.AccessionNumber == "" and back.PatientIdentityRemoved == "YES"
    assert back.StudyInstanceUID != st.study_uid
    assert rdsr.build(st, SECRET).SOPInstanceUID == ds.SOPInstanceUID
    assert p.device["manufacturer"] == st.device.manufacturer
    stored = {e.event_uid for e in st.events}
    assert not stored & {e["event_uid"] for e in p.events}


def test_projection_roundtrip_identified(session):
    st = _store(session, f.proj_rdsr(study_uid="5.2.1", sop_uid="5.2.1.1"))
    back, p = _roundtrip(rdsr.build(st, SECRET, identified=True))
    assert p.kind == "proj_rdsr" and back.StudyInstanceUID == st.study_uid
    assert back.PatientID == st.patient.patient_id
    for o, ev in zip(_ordered(st), p.events, strict=True):
        for k in FIELDS_PROJ:
            want, got = getattr(o, k), ev[k]
            assert got == pytest.approx(want) if isinstance(want, float) else got == want, k
    assert p.study["modality"] == st.modality


def test_header_study_and_estimated_weight(session, tmp_path):
    st = _store(session, f.dx_image(study_uid="5.3.1", sop_uid="5.3.1.1"))
    st.weight_kg, st.weight_source = 25.0, SizeSource.estimated_reference
    session.flush()
    paths = rdsr.write_studies(session, [st.id], tmp_path, SECRET)
    back = pydicom.dcmread(paths[0])
    p = parse(back)
    assert p.kind == "proj_rdsr" and p.events[0]["dap"] == pytest.approx(st.events[0].dap)
    assert p.events[0]["event_uid"] != st.events[0].event_uid  # the image SOP UID
    same = parse(rdsr.build(st, SECRET, identified=True))
    assert same.events[0]["event_uid"] == st.events[0].event_uid
    assert "PatientWeight" not in back and p.study["weight_kg"] is None


def test_rrdsr_roundtrip_for_pet(session):
    """A PET study's administrations -> RRDSR (TID 10021) -> parsed back the same."""
    st = _store(session, f.pet_image(study_uid="5.4.1", sop_uid="5.4.1.1", mbq=210.0))
    ds = rdsr.build(st, SECRET)
    assert ds.SOPClassUID == rdsr.RRDSR_SOP
    assert ds.ContentTemplateSequence[0].TemplateIdentifier == "10021"
    back, p = _roundtrip(ds)
    assert p.kind == "nm_header" and len(p.events) == 1
    ev, stored = p.events[0], st.events[0]
    assert ev["extra_json"]["activity_mbq"] == pytest.approx(210.0)
    assert ev["protocol"] == stored.protocol and ev["comment"] == stored.comment
    assert ev["event_datetime"] == stored.event_datetime
    assert ev["extra_json"]["half_life_s"] == pytest.approx(6586.2)
    assert p.study["modality"] == "PT" and back.PatientIdentityRemoved == "YES"
    # a received RRDSR is stored like the image headers: same activity on the study
    other = Store(session, SECRET).store(p)
    assert session.get(Study, other.study_id).admin_activity_mbq == pytest.approx(210.0)
