"""Quarterly INSP report (Order 1255/2016): counting, bands, sex, mean dose, layout."""

import datetime as dt
import io

import pytest

from dosimetrix import classify
from dosimetrix.db.models import Event as Ev
from dosimetrix.db.models import Patient, Study
from dosimetrix.reports import insp1255
from dosimetrix.seeds.loader import load_all

Y2 = int(2 * 365.25) + 20  # 2 years
Y7 = int(7 * 365.25) + 20


def test_quarter_range():
    assert insp1255.quarter_range(2026, 1) == (dt.datetime(2026, 1, 1), dt.datetime(2026, 4, 1))
    assert insp1255.quarter_range(2026, 4) == (dt.datetime(2026, 10, 1), dt.datetime(2027, 1, 1))
    with pytest.raises(ValueError):
        insp1255.quarter_range(2026, 5)


@pytest.fixture
def report(session):
    load_all(session)
    boy, girl = Patient(sex="M", pseudo_id="P-1"), Patient(sex="F", pseudo_id="P-2")
    session.add_all([boy, girl])
    session.flush()
    t = dt.datetime(2026, 2, 10, 9)

    def dx(uid, pat, age, events, when=t):
        s = Study(study_uid=uid, modality="DX", study_datetime=when, patient_id=pat.id,
                  age_days=age, source_kind="test", study_description="Chest")
        s.events = events
        session.add(s)

    # boy, 2 y: chest AP twice (retake) + LAT -> one T1 exam (0.03) and one T2 exam
    dx("r.1", boy, Y2, [Ev(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.01),
                        Ev(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.02),
                        Ev(protocol="W035 Torace lat", view="lateral", dap=0.05)])
    # girl, 7 y: chest AP
    dx("r.2", girl, Y7, [Ev(protocol="W035 Torace p.a.", view="postero-anterior", dap=0.04)])
    # outside the quarter
    dx("r.3", girl, Y7, [Ev(protocol="W035 Torace p.a.", view="postero-anterior", dap=9.0)],
       when=dt.datetime(2026, 4, 1))
    cath = Study(study_uid="r.5", modality="RF", study_datetime=t, patient_id=boy.id,
                 age_days=Y2, source_kind="test", total_dap=2.0, total_rp_dose=0.05,
                 study_description="Pediatric^Pediatric Cardiac Intervention")
    opg = Study(study_uid="r.6", modality="DX", study_datetime=t, patient_id=girl.id,
                age_days=Y7, source_kind="test", study_description="Ortopantomografie")
    opg.events = [Ev(protocol="Ortopantomografie", dap=0.08)]
    session.add_all([cath, opg])
    ct = Study(study_uid="r.4", modality="CT", study_datetime=t, patient_id=girl.id,
               age_days=Y7, source_kind="test", total_dlp=400.0,
               study_description="Head^CAP_SPIRAL_CONTRAST (Child)")
    session.add(ct)
    session.flush()
    classify.run(session)
    start, end = insp1255.quarter_range(2026, 1)
    return insp1255.build(session, [], start, end)


def _row(report, sheet, code):
    s = next(x for x in report["sheets"] if x.key == sheet)
    return next(r for r in s.rows if r["code"] == code)


def test_counts_bands_and_means(report):
    t1 = _row(report, "radiologie", "T1")
    assert t1["total"] == 2
    assert t1["counts"]["1-4y"] == {"M": 1, "F": 0} and t1["counts"]["5-9y"] == {"M": 0, "F": 1}
    assert t1["means"]["dap"]["1-4y"] == pytest.approx(0.03)  # the retake counts
    assert t1["means"]["dap"]["5-9y"] == pytest.approx(0.04)  # April is outside Q1
    assert _row(report, "radiologie", "T2")["total"] == 1
    assert _row(report, "radiologie", "C1")["total"] == 0  # every code listed, even empty
    csc = _row(report, "ct", "CSC")  # contrast head CT
    assert csc["total"] == 1 and csc["means"]["dlp"]["5-9y"] == 400.0
    assert _row(report, "ct", "C")["total"] == 0


def test_json_and_xlsx(report):
    j = insp1255.as_json(report)
    assert j["period"] == {"from": "2026-01-01", "to": "2026-03-31"}
    rx = j["sheets"][0]
    assert rx["count_bands"][0]["label"].startswith("0") and len(rx["mean_bands"]) == 5
    assert [s["key"] for s in j["sheets"]] == ["radiologie", "ct", "interventionala_rx",
                                               "interventionala_ct", "dentare"]
    buf = io.BytesIO()
    insp1255.write_xlsx(report, buf, {"Generat": "test"})
    import zipfile

    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        book = z.read("xl/workbook.xml").decode()
        rx = z.read("xl/worksheets/sheet2.xml").decode()
    assert "Informatii" in book and "Radiologie" in book and ">CT<" not in rx
    assert "<t>T1</t>" in rx and "ESAK sau MGD" in rx


def test_interventional_and_dental_sheets(report):
    altri = _row(report, "interventionala_rx", "ALTRI")
    assert altri["total"] == 1 and altri["counts"]["0-4y"] == {"M": 1, "F": 0}
    assert altri["means"]["dap"]["0-4y"] == 2.0
    assert altri["means"]["kar"]["0-4y"] == pytest.approx(50.0)  # mGy
    d2 = _row(report, "dentare", "D2")
    assert d2["total"] == 1 and d2["means"]["dap"]["5-9y"] == pytest.approx(0.08)
