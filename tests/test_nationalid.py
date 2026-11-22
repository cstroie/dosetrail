"""National IDs (synthetic CNPs only) and the CNP > HIS > DICOM precedence."""

import datetime as dt

from dosimetrix import demographics, nationalid
from dosimetrix.db.models import AgePrecision, Patient, Study
from dosimetrix.ingest.parse import parse
from dosimetrix.ingest.store import Store
from dosimetrix.nationalid import ro_cnp, ro_cnp_check_digit
from tests import dicom_factory as f

SECRET = b"t" * 32
ON = dt.date(2026, 11, 22)


def cnp(first12: str) -> str:
    return first12 + str(ro_cnp_check_digit(first12))


def test_ro_cnp_sex_and_century():
    assert ro_cnp(cnp("518010140001"), ON) == nationalid.NationalId("ro_cnp", "M",
                                                                     dt.date(2018, 1, 1))
    assert ro_cnp(cnp("285123140002"), ON).birth_date == dt.date(1985, 12, 31)
    assert ro_cnp(cnp("285123140002"), ON).sex == "F"
    assert ro_cnp(cnp("399060140003"), ON).birth_date == dt.date(1899, 6, 1)


def test_ro_cnp_rejects():
    good = cnp("518010140001")
    bad_check = good[:12] + str((int(good[12]) + 1) % 10)
    assert ro_cnp(bad_check, ON) is None
    assert ro_cnp(cnp("518023040001"), ON) is None  # 30 February
    assert ro_cnp(cnp("518010160001"), ON) is None  # county 60
    assert ro_cnp(cnp("018010140001"), ON) is None
    assert ro_cnp("51801014000", ON) is None
    assert ro_cnp("P1", ON) is None
    assert nationalid.parse(None) is None


def test_ro_cnp_future_birth_is_dropped():
    assert ro_cnp(cnp("527010140001"), ON).birth_date is None  # 2027: not born yet


def test_ro_cnp_residents_century():
    assert ro_cnp(cnp("750010140001"), ON).birth_date == dt.date(1950, 1, 1)  # 2050 is future
    assert ro_cnp(cnp("810010140001"), ON).birth_date == dt.date(2010, 1, 1)  # under 100
    assert ro_cnp(cnp("810010140001"), ON).sex == "F"
    assert ro_cnp(cnp("910010140001"), ON).sex is None


def test_reconcile_precedence():
    p = Patient(patient_id=cnp("518010140001"), sex="F", birth_date=dt.date(2018, 1, 2))
    assert demographics.reconcile(p, on=ON)
    assert (p.sex, p.birth_date, p.demo_source) == ("M", dt.date(2018, 1, 1), "cnp")
    assert "sex dicom=F vs cnp=M" in p.demo_conflict
    # without a national ID: HIS beats what is stored
    q = Patient(patient_id="P1", sex="F", birth_date=dt.date(2018, 1, 2))
    assert demographics.reconcile(q, his_sex="male", his_birth=dt.date(2018, 1, 3))
    assert (q.sex, q.birth_date) == ("M", dt.date(2018, 1, 3))
    assert q.demo_source == "his"
    # nothing new: unchanged, no conflict
    r = Patient(patient_id="P2", sex="O")
    assert demographics.reconcile(r, dicom_sex="O") is False and r.sex == "O"


def test_parent_cnp_is_not_applied_to_a_child():
    mother = cnp("290031540004")  # born 1990-03-15
    baby = Patient(patient_id=mother, sex="M", birth_date=dt.date(2026, 10, 2))
    assert demographics.reconcile(baby, on=ON) is False
    assert (baby.sex, baby.birth_date) == ("M", dt.date(2026, 10, 2))
    assert "another person" in baby.demo_conflict
    # a 1 January header date is a placeholder: the CNP still wins
    adult = Patient(patient_id=mother, sex="F", birth_date=dt.date(1950, 1, 1))
    assert demographics.reconcile(adult, on=ON) and adult.birth_date == dt.date(1990, 3, 15)


def test_ingest_takes_sex_and_age_from_cnp(session):
    pid = cnp("518010140001")  # boy born 2018-01-01; the header says F, born 2018-01-01
    store = Store(session, SECRET)
    r = store.store(parse(f.dx_image(study_uid="1.2.3.900", sop_uid="1.2.3.900.1",
                                     patient_id=pid, birth="")))
    st = session.get(Study, r.study_id)
    assert st.patient.sex == "M" and st.patient.birth_date == dt.date(2018, 1, 1)
    assert st.age_precision == AgePrecision.exact
    assert st.age_days == (dt.date(2025, 11, 6) - dt.date(2018, 1, 1)).days
    assert "sex" in st.patient.demo_conflict


def test_backfill_corrects_and_reages(session):
    pid = cnp("518010140001")
    pat = Patient(patient_id=pid, sex="F", birth_date=dt.date(2018, 6, 1),
                  legacy_sha256="x" * 64, pseudo_id="P-TEST0-00001")
    session.add(pat)
    session.flush()
    st = Study(study_uid="1.2.3.901", modality="DX", patient_id=pat.id, source_kind="test",
               study_datetime=dt.datetime(2025, 1, 1, 10), age_days=1, n_events=0)
    session.add(st)
    session.flush()
    counts, studies = demographics.backfill(session)
    assert counts["sex_corrected"] == 1 and counts["birth_corrected"] == 1
    assert studies == [st.id]
    assert st.age_days == (dt.date(2025, 1, 1) - dt.date(2018, 1, 1)).days
