from __future__ import annotations

import datetime as dt
import io
import zipfile

import pytest

from dosimetrix import classify, nationalid
from dosimetrix.db.models import Device, Patient, Study
from dosimetrix.db.models import Event as Ev
from dosimetrix.reports import art12
from dosimetrix.seeds.loader import load_all

Y2, Y7, ADULT = 730, 2557, 14000


def test_cnp7_from_stored_cnp_or_sex_and_birth_date():
    first12 = "506031512345"
    cnp = first12 + str(nationalid.ro_cnp_check_digit(first12))
    assert art12.cnp7(cnp, None, None) == "5060315"  # a valid CNP keeps its first 7
    assert art12.cnp7("X-123", "F", dt.date(2019, 3, 5)) == "6190305"  # rebuilt: 2000s, female
    assert art12.cnp7(None, "M", dt.date(1985, 12, 24)) == "1851224"
    assert art12.cnp7(None, "M", None) == "" and art12.cnp7(None, None, dt.date(2000, 1, 1)) == ""


def test_pregnancy_only_for_women_of_childbearing_age():
    assert art12._pregnancy("F", 30 * 365, True, 12) == "Da, 12 sapt"
    assert art12._pregnancy("F", 30 * 365, False, None) == "Nu"
    assert art12._pregnancy("F", 30 * 365, None, None) == ""
    assert art12._pregnancy("M", 30 * 365, False, None) == ""
    assert art12._pregnancy("F", 5 * 365, False, None) == ""


@pytest.fixture
def books(session):
    load_all(session)
    dev = Device(display_name="Scanner A", identity_key="a", manufacturer="ACME",
                 model_name="X1", serial_number="123", is_local=True)
    other = Device(display_name="Mobile B", identity_key="b", is_local=True)
    session.add_all([dev, other])
    girl = Patient(sex="F", pseudo_id="P-1", birth_date=dt.date(2019, 3, 5))
    session.add(girl)
    session.flush()

    def study(uid, mod, when, device=dev, **kw):
        s = Study(study_uid=uid, modality=mod, study_datetime=when, patient_id=girl.id,
                  device_id=device.id, age_days=Y7, source_kind="test", **kw)
        session.add(s)
        return s

    feb = dt.datetime(2026, 2, 10, 8)
    for i in range(25):  # 25 head CTs, contrast: only the first 20 go on the sheet
        study(f"ct.{i}", "CT", feb + dt.timedelta(hours=i), total_dlp=100.0 + i,
              study_description="Head^CAP_SPIRAL_CONTRAST (Child)", indication_text="cefalee")
    study("ct.march", "CT", dt.datetime(2026, 4, 1), total_dlp=1.0,
          study_description="Head^CAP_SPIRAL_CONTRAST (Child)")  # outside the window
    dx = study("dx.1", "DX", feb, device=other, study_description="Chest")
    dx.events = [Ev(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.01),
                 Ev(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.02),
                 Ev(protocol="W035 Torace lat", view="lateral", dap=0.05)]
    nm = study("nm.1", "NM", feb, study_description="Scintigrafie renala",
               admin_activity_mbq=40.0)
    nm.events = [Ev(event_type="radiopharmaceutical", protocol="DMSA",
                    extra_json={"activity_mbq": 40.0})]
    session.flush()
    classify.run(session)
    return art12.build(session, 2026)


def _sheet(books, family, device, name):
    b = next(b for b in books if b.family == family and b.device_name == device)
    return next(s for s in b.sheets if s.name == name)


def test_twenty_patients_per_sheet_in_the_window(books):
    cap = _sheet(books, "ct", "Scanner A", "Cap")
    assert cap.available == 25 and cap.patients == 20 and len(cap.rows) == 20
    assert cap.rows[0][:8] == [1, "6190305", 7, "F", "", "", "", "cefalee"]
    assert cap.rows[0][8:] == ["contrast", 100.0]  # the first in time order
    assert cap.rows[19][9] == 119.0  # the 20th; the 21st to 25th and April are left out


def test_radiography_sums_retakes_per_projection_and_split_by_device(books):
    chest = _sheet(books, "radiografie", "Mobile B", "TORACE CP")
    assert [r[-2:] for r in chest.rows] == [["AP/PA", 0.03], ["LAT", 0.05]]
    assert not any(b.device_name == "Scanner A" and b.family == "radiografie" for b in books)


def test_nuclear_medicine_row_and_general_sheet(books):
    nm = _sheet(books, "nm_diagnostic", "Scanner A", "SCINTIGRAFIE RENALA")
    assert nm.rows[0][8:] == ["Tc-99m", "DMSA", "SR4", 40.0, ""]  # no coefficient: blank mSv
    b = next(b for b in books if b.family == "ct")
    assert b.info["Producator"] == "ACME" and b.info["Tip si model echipament"] == "X1"
    buf = io.BytesIO()
    art12.write_zip(books, buf)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        assert any(n.startswith("Art12_CT_DLP_Scanner_A") for n in z.namelist())
    s = art12.summary(books)
    cap = next(x for x in s if x["family"] == "ct")["sheets"][0]
    assert cap == {"name": "Cap", "patients": 20, "available": 25, "missing": 0}


def test_every_seeded_code_exists_in_the_national_systems(session):
    from sqlalchemy import select

    from dosimetrix.db.models import Code, CodeSystem

    load_all(session)
    for fam in art12.seed():
        have = set(session.scalars(select(Code.code).join(Code.system)
                                   .where(CodeSystem.key == fam["system"])))
        for s in fam["sheet"]:
            assert set(s["codes"]) <= have, (fam["key"], s["name"], set(s["codes"]) - have)
