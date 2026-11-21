"""Exports: the XLSX writer (checked by reading the XML back) and list rows."""

import datetime as dt
import io
import re
import zipfile

import pytest

from dosetrail import classify
from dosetrail.db.models import Device, Event, Patient, Study
from dosetrail.export import lists
from dosetrail.export.xlsx import MAX_ROWS, XlsxWriter, col_name
from dosetrail.seeds.loader import load_all


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
    import dosetrail.export.xlsx as x

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
    assert r["Exam code"] == "RG-TH" and r["DAP total [Gy.cm2]"] == 0.03
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

    from dosetrail.export import drl_dataset as ds

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
    assert doc["dataset"] == "dosetrail-drl" and doc["rows_total"] == 2
    assert {d["name"] for d in doc["dictionary"]} == set(ds.FIELDS)
    buf = io.BytesIO()
    ds.write("csv", buf, iter(rows), meta)
    assert buf.getvalue().decode("utf-8-sig").splitlines()[0].startswith("dataset_version,")
    buf = io.BytesIO()
    ds.write("xlsx", buf, iter(rows), meta)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert "xl/worksheets/sheet3.xml" in z.namelist()  # Data, Dictionary, About


def test_icrp135_summary_rows(session, data):
    from dosetrail.export import drl_dataset as ds

    rows = ds.summary_rows(session, [], "ro-ms838-2025", min_n=1)
    ap = [r for r in rows if r["exam"] == "RX-TORACE-PA-AP"]
    assert {r["unit"] for r in ap} == {"device", "facility"}
    fac = next(r for r in ap if r["unit"] == "facility")
    assert fac["n"] == 1 and fac["median"] == 0.01 and fac["sample_ok"] is True
    assert fac["drl"] == 0.11 and fac["status"] == "below"  # 5-9 y chest PA/AP
