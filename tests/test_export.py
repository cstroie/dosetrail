"""Exports: the XLSX writer (checked by reading the XML back) and list rows."""

import datetime as dt
import io
import re
import zipfile

import pytest

from dosimetrix import classify
from dosimetrix.db.models import Device, Event, Patient, Study
from dosimetrix.export import lists
from dosimetrix.export.xlsx import MAX_ROWS, XlsxWriter, col_name
from dosimetrix.seeds.loader import load_all


def _sheet_xml(data: bytes, n: int = 1) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/styles.xml",
                "xl/_rels/workbook.xml.rels"} <= names
        return z.read(f"xl/worksheets/sheet{n}.xml").decode()


def test_xlsx_cells_types_and_sheets():
    assert [col_name(i) for i in (0, 25, 26, 701, 702)] == ["A", "Z", "AA", "ZZ", "AAA"]
    buf = io.BytesIO()
    with XlsxWriter(buf) as wb:
        ws = wb.sheet("Data/1", ["Date", "Value", "Text", "Flag"], widths=[12, 8, 20, 6])
        ws.row([dt.date(2026, 1, 2), 1.5, "a<b & \x01c", True])
        ws.row([dt.datetime(1900, 3, 1, 12, 0), 7, None, False])
        wb.sheet("Second", ["n"]).row([float("nan")])
    xml = _sheet_xml(buf.getvalue())
    assert '<c r="A2" s="2"><v>46024</v></c>' in xml  # 2026-01-02 as an Excel serial
    assert '<c r="B2"><v>1.5</v></c>' in xml
    assert "a&lt;b &amp; c" in xml and "\x01" not in xml  # escaped, control char dropped
    assert '<c r="D2" t="b"><v>1</v></c>' in xml
    assert '<c r="A3" s="3"><v>61.50000000</v></c>' in xml
    assert 'state="frozen"' in xml and '<autoFilter ref="A1:D3"/>' in xml
    assert "<v>nan</v>" not in _sheet_xml(buf.getvalue(), 2)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        book = z.read("xl/workbook.xml").decode()
    assert re.findall(r'<sheet name="([^"]+)"', book) == ["Data1", "Second"]


def test_xlsx_row_limit(monkeypatch):
    import dosimetrix.export.xlsx as x

    monkeypatch.setattr(x, "MAX_ROWS", 3)
    with XlsxWriter(io.BytesIO()) as wb:
        ws = wb.sheet("S", ["n"])
        ws.row([1])
        ws.row([2])
        with pytest.raises(OverflowError):
            ws.row([3])
    assert MAX_ROWS == 1_048_576


@pytest.fixture
def data(session):
    load_all(session)
    dev = Device(display_name="Room 1", identity_key="x-1", is_local=True)
    pat = Patient(patient_id="123", name="POP^ION", pseudo_id="P-AAAAA-BBBBB", sex="M",
                  birth_date=dt.date(2020, 1, 1))
    session.add_all([dev, pat])
    session.flush()
    s = Study(study_uid="x.1", modality="DX", device_id=dev.id, patient_id=pat.id,
              study_datetime=dt.datetime(2026, 3, 1, 10), study_description="Chest",
              source_kind="test", age_days=2251, total_dap=0.03, n_events=2)
    s.events = [Event(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.01, kvp=70),
                Event(protocol="W035 Torace lat", view="lateral", dap=0.02, kvp=75)]
    session.add(s)
    session.flush()
    classify.run(session)
    return s


def test_rows_pseudonymous_by_default(session, data):
    cols = [h for h, _ in lists.columns("study", False)]
    (row,) = list(lists.rows(session, [], "study"))
    r = dict(zip(cols, row, strict=True))
    assert r["Patient"] == "P-AAAAA-BBBBB" and "Patient ID" not in r
    assert r["Exam code"] == "RG-TH" and r["DAP total [mGy.cm2]"] == 30
    assert r["Device"] == "Room 1" and r["Origin"] == "local" and r["Age"] == "6 y"
    assert "123" not in [str(v) for v in row] and "POP^ION" not in row

    pid_cols = [h for h, _ in lists.columns("study", True)]
    (prow,) = list(lists.rows(session, [], "study", include_pid=True))
    p = dict(zip(pid_cols, prow, strict=True))
    assert p["Patient ID"] == "123" and p["Patient name"] == "POP^ION"


def test_event_rows_carry_projection_drl_codes(session, data):
    cols = [h for h, _ in lists.columns("event", False)]
    out = [dict(zip(cols, r, strict=True)) for r in lists.rows(session, [], "event")]
    assert [(r["DRL exam (national)"], r["kVp [kV]"]) for r in out] == [
        ("RX-TORACE-PA-AP", 70.0), ("RX-TORACE-LAT", 75.0)]
    buf = io.StringIO()
    assert lists.write_csv(buf, "event", False, iter([])) == 0
    assert buf.getvalue().startswith("Study ID,Date,")


def test_drl_dataset_rows_and_formats(session, data):
    import json

    from dosimetrix.export import drl_dataset as ds

    rows = list(ds.dataset_rows(session, [], b"k" * 32))
    assert [(r["drl_exam_national"], r["projection"], r["dap"]) for r in rows] == [
        ("RX-TORACE-PA-AP", "AP", 0.01), ("RX-TORACE-LAT", "LL", 0.02)]
    r = rows[0]
    assert r["period"] == "2026-03" and r["age_group"] == "5-9y" and r["exam_code"] == "RG-TH"
    assert r["patient"].startswith("P-") and r["patient"] != "P-AAAAA-BBBBB"  # survey code
    assert r["device"].startswith("D-") and r["facility"].startswith("F-")
    assert set(r) == set(ds.FIELDS)
    assert rows[0]["patient"] == list(ds.dataset_rows(session, [], b"k" * 32))[0]["patient"]
    assert rows[0]["patient"] != list(ds.dataset_rows(session, [], b"z" * 32))[0]["patient"]

    meta = ds.metadata("q", dt.datetime(2026, 4, 1))
    buf = io.BytesIO()
    assert ds.write("json", buf, iter(rows), meta) == 2
    doc = json.loads(buf.getvalue())
    assert doc["dataset"] == "dosimetrix-drl" and doc["rows_total"] == 2
    assert {d["name"] for d in doc["dictionary"]} == set(ds.FIELDS)
    buf = io.BytesIO()
    ds.write("csv", buf, iter(rows), meta)
    assert buf.getvalue().decode("utf-8-sig").splitlines()[0].startswith("dataset_version,")
    buf = io.BytesIO()
    ds.write("xlsx", buf, iter(rows), meta)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert "xl/worksheets/sheet3.xml" in z.namelist()  # Data, Dictionary, About


def test_icrp135_summary_rows(session, data):
    from dosimetrix.export import drl_dataset as ds

    rows = ds.summary_rows(session, [], "ro-ms838-2025", min_n=1)
    ap = [r for r in rows if r["exam"] == "RX-TORACE-PA-AP"]
    assert {r["unit"] for r in ap} == {"device", "facility"}
    fac = next(r for r in ap if r["unit"] == "facility")
    assert fac["n"] == 1 and fac["median"] == 0.01 and fac["sample_ok"] is True
    assert fac["drl"] == 0.11 and fac["status"] == "below"  # 5-9 y chest PA/AP


def test_survey_rows_paediatric_series_and_checks(session):
    from dosimetrix.export import survey

    load_all(session)
    kid = Patient(pseudo_id="P-K", sex="F")
    session.add(kid)
    session.flush()
    t = dt.datetime(2026, 10, 5, 9)
    head = Study(study_uid="v.1", modality="CT", study_datetime=t, patient_id=kid.id,
                 age_days=300, weight_kg=10.0, source_kind="test",
                 study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
    head.events = [Event(protocol="Topogram", is_localizer=True, dlp=1.0),
                   Event(protocol="Head", ctdivol=20.0, scan_length_mm=100.0, dlp=200.0,
                         ct_phantom_cm=16, mas=150.0, pitch=0.6),
                   Event(protocol="Head 2", ctdivol=20.0, scan_length_mm=100.0, dlp=400.0,
                         ct_phantom_cm=32)]
    adult = Study(study_uid="v.2", modality="CT", study_datetime=t, age_days=40 * 366,
                  source_kind="test", study_description="Head^HeadRoutine (Adult)")
    adult.events = [Event(protocol="Head", ctdivol=50.0, dlp=900.0)]
    rx = Study(study_uid="v.3", modality="DX", study_datetime=t, patient_id=kid.id,
               age_days=400, source_kind="test", study_description="Chest")
    rx.events = [Event(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.01,
                       kvp=60.0),
                 Event(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.02,
                       kvp=64.0)]
    session.add_all([head, adult, rx])
    session.flush()
    classify.run(session)

    out = list(survey.rows(session, [], b"s" * 32))
    assert {r["modality"] for r in out} == {"CT", "DX"}  # the adult is excluded
    ct = [r for r in out if r["modality"] == "CT"]
    assert [r["series"] for r in ct] == [1, 2]  # localiser excluded
    assert ct[0]["head_age_group"] == "3mo-1y" and ct[0]["weight_group"] == "5-15kg"
    assert ct[0]["edrl_exam"] == "CT-HEAD" and ct[0]["effective_mas"] == 250.0
    assert ct[0]["check_dlp"] == "ok" and ct[0]["check_phantom"] == "ok"
    assert ct[1]["check_dlp"] == "fail" and ct[1]["check_phantom"] == "warn"
    assert ct[0]["period"] == "2026-10" and ct[0]["patient"].startswith("P-")
    (dx,) = [r for r in out if r["modality"] == "DX"]
    assert dx["pka"] == pytest.approx(30.0) and dx["exposures"] == 2  # mGy.cm2
    assert dx["projection"] == "AP" and dx["kvp"] == 62.0 and dx["ndrl_exam"] == "RX-TORACE-PA-AP"
    assert set(ct[0]) == set(survey.FIELDS)
    assert survey.month_range("2026-12") == (dt.datetime(2026, 12, 1), dt.datetime(2027, 1, 1))


def test_ages_are_coarsened_and_codes_are_stable():
    from types import SimpleNamespace

    from dosimetrix.export import drl_dataset as ds

    assert ds.coarse_age(200) == {"age_days": 200, "age_months": 6, "age_years": 0}
    assert ds.coarse_age(500) == {"age_days": None, "age_months": 16, "age_years": 1}
    assert ds.coarse_age(3000) == {"age_days": None, "age_months": None, "age_years": 8}
    assert ds.coarse_age(None)["age_years"] is None
    a = SimpleNamespace(id=1, legacy_sha256="abc")
    b = SimpleNamespace(id=99, legacy_sha256="abc")  # the same patient after a re-import
    assert ds.patient_code(b"k" * 32, a) == ds.patient_code(b"k" * 32, b)
    assert ds.patient_code(b"k" * 32, None) is None


def test_csv_cells_are_spreadsheet_safe():
    assert lists.safe_text("- - - - -") == "'- - - - -"
    assert lists.safe_text("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert lists.safe_text("Chest") == "Chest" and lists.safe_text(-30.0) == -30.0
    assert lists._csv_value("@x") == "'@x"
