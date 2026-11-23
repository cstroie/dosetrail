from dosimetrix.ingest import dosepage

PAGE1 = """SPIT. TEST/AQUILION Aquilion PRIME
Patient ID: 1234567890123
Patient Name (Country): TEST PATIENT
Patient Name (Multi-byte) :
Patient Info : 2009/05/25 / 11 / Man / 45(kg)
Study Date : 2021/04/14
Dose Display : IEC 3.0
Total DLP(mGy.cm) : (Head): - (Body): 258.00
P.1/3"""

PAGE2 = """Patient ID : 1234567890123 Study Date :2021/04/14
1.Cardiac CTA/CFA
#of CTDIvol DLP
No. Protocol scan(s) kVp (mGy) (mGy.cm)
1 DualScano 1 120
2 DualScano 1 120
2.90 0.60
3 S&V 1 100 (Body) (Body)
SURE 5.30 1.10
4 Start 1 80 (Body) (Body)
. 8.40 199.00
5 Helical 1 80 (Body) (Body)
. 2.30 57.30
6 Helical 1 80 (Body) (Body)
SUREExposure
SURE Image Dose
No. Name SD IQ Thickness Recon FC Reduction XY
5 CTAStandard 33.00 Cardiac:CTAS... 0.5 FCO3 ne ECG
P.2/3"""


def test_summary_page():
    p = dosepage.parse_text(PAGE1)
    assert p["patient_id"] == "1234567890123" and p["study_date"] == "20210414"
    assert p["weight_kg"] == 45.0 and p["total_dlp"] == 258.0 and p["rows"] == []


def test_table_page_reads_every_acquisition():
    p = dosepage.parse_text(PAGE2)
    rows = p["rows"]
    assert [r["no"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert [(r["ctdivol"], r["dlp"]) for r in rows] == [
        (None, None), (None, None), (2.9, 0.6), (5.3, 1.1), (8.4, 199.0), (2.3, 57.3)]
    assert rows[3]["protocol"] == "SUREStart" and rows[4]["kvp"] == 80 and rows[4]["phantom"] == 32
    assert round(sum(r["dlp"] or 0 for r in rows), 2) == 258.0  # page 1's total
    assert [dosepage.event_type(r["protocol"]) for r in rows] == [
        "localizer", "localizer", "axial", "axial", "spiral", "spiral"]


def test_unrelated_text_is_not_a_dose_page():
    assert dosepage.parse_text("EP Name : Cardiac\nContrast Name : CE\nP.3/3") is None


def test_event_uid_is_stable_and_per_row():
    a = dosepage.event_uid("1.2.3", 1, 5)
    assert a == dosepage.event_uid("1.2.3", 1, 5) and a != dosepage.event_uid("1.2.3", 1, 6)
    assert len(a) <= 64


# ------------------------------------------------------- parse + store (OCR faked)

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from dosimetrix.db.models import Study  # noqa: E402
from dosimetrix.ingest.parse import parse  # noqa: E402
from dosimetrix.ingest.store import Store  # noqa: E402
from tests import dicom_factory as f  # noqa: E402


def _page(monkeypatch, text, sop="1.2.3.700.50", pid=None, date=None):
    ds = f.ct_series_image(study_uid="1.2.3.700", sop_uid=sop, series_uid="1.2.3.700.9",
                           ctdivol=8.4, count=696, spacing=0.5)  # 8.4 x 34.8 cm = 292
    ds.PatientID = pid or "1234567890123"
    ds.StudyDate = date or "20210414"
    ds.SOPClassUID = dosepage.SECONDARY_CAPTURE
    ds.Manufacturer = "TOSHIBA"
    monkeypatch.setattr(dosepage, "read", lambda _ds, t=text: dosepage.parse_text(t))
    return ds


def test_dose_page_table_gives_one_event_per_acquisition(monkeypatch):
    p = parse(_page(monkeypatch, PAGE2))
    assert p.kind == "dose_page" and p.study["modality"] == "CT" and len(p.events) == 6
    helical = p.events[4]
    assert helical["ctdivol"] == 8.4 and helical["dlp"] == 199.0 and helical["event_type"] == "spiral"
    assert helical["extra_json"]["source"] == "ocr" and p.events[0]["is_localizer"]


def test_summary_page_gives_the_weight_only(monkeypatch):
    p = parse(_page(monkeypatch, PAGE1, sop="1.2.3.700.51"))
    assert p.kind == "dose_page" and p.events == [] and p.study["weight_kg"] == 45.0


def test_another_patient_or_day_is_not_trusted(monkeypatch):
    assert parse(_page(monkeypatch, PAGE2, pid="999")).kind == "ignored"
    assert parse(_page(monkeypatch, PAGE2, date="20210415")).kind == "ignored"
    bad = PAGE2.replace("199.00", "1999.00")  # a misread: 237 cm of scan
    assert parse(_page(monkeypatch, bad)).kind == "ignored"


def test_dose_page_replaces_header_estimates_in_either_order(session, monkeypatch):
    store = Store(session, b"x" * 32)
    # headers first: the estimate (8.4 x 34.8 cm = 292) is replaced by the scanner's values
    assert store.store(parse(f.ct_series_image(study_uid="1.2.3.700", sop_uid="1.2.3.700.1",
                                               series_uid="1.2.3.700.9", ctdivol=8.4, count=696,
                                               spacing=0.5))).status == "imported"
    st = session.scalar(select(Study).filter_by(study_uid="1.2.3.700"))
    assert st.total_dlp == pytest.approx(292.32)
    store.store(parse(_page(monkeypatch, PAGE1, sop="1.2.3.700.51")))
    store.store(parse(_page(monkeypatch, PAGE2)))
    session.refresh(st)
    assert st.n_events == 6 and st.total_dlp == pytest.approx(258.0)
    assert st.source_kind == "dose_page" and 45.0 in (st.weight_kg, st.weight_reported_kg)
    # a header arriving afterwards is superseded; the page again is a duplicate
    assert store.store(parse(f.ct_series_image(study_uid="1.2.3.700", sop_uid="1.2.3.700.2",
                                               series_uid="1.2.3.700.8"))).status == "superseded"
    assert store.store(parse(_page(monkeypatch, PAGE2))).status == "duplicate"
