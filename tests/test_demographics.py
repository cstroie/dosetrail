"""demographics.py: patient sex / birth date precedence and study ages."""

from __future__ import annotations

from datetime import date, datetime

from dozimetrix import demographics
from dozimetrix.db.models import AgePrecision, Patient, Study

CNP = "1800315123454"  # valid Romanian CNP: male, born 1980-03-15


def test_age_precision_values():
    assert {p.value for p in AgePrecision} == {"exact", "dicom_age", "estimated", "unknown"}


def test_norm_sex():
    from dozimetrix.demographics import _norm_sex

    assert _norm_sex("M") == "M"
    assert _norm_sex("f") == "F"
    assert _norm_sex(" male ") == "M"
    assert _norm_sex("O") is None  # "other" is only kept when nothing else is known
    assert _norm_sex("") is None
    assert _norm_sex(None) is None


def test_real_birth_date():
    from dozimetrix.demographics import _real

    assert _real(date(1980, 3, 15)) is True
    assert _real(date(1980, 1, 1)) is False  # 1 January: age-only placeholder
    assert _real(date(1753, 6, 1)) is False  # before 1901: "unknown" / typo
    assert _real(date(1901, 6, 1)) is True
    assert _real(None) is False


def test_foreign_birth_date():
    from dozimetrix.demographics import _foreign

    headers = [date(2020, 5, 1), date(2021, 6, 15)]
    assert _foreign(date(1980, 3, 1), headers) == date(2020, 5, 1)
    assert _foreign(date(2020, 2, 1), headers) is None
    assert _foreign(None, headers) is None


def test_another_person():
    pat = Patient(patient_id="PAT-001", sex="M", birth_date=date(2020, 5, 10))
    assert demographics.another_person(pat, date(1980, 3, 1)) is True
    assert demographics.another_person(pat, date(2020, 6, 1)) is False
    pat.birth_date = date(2020, 1, 1)  # placeholder: says nothing
    assert demographics.another_person(pat, date(1980, 3, 1)) is False


def test_reconcile_cnp_wins():
    pat = Patient(patient_id=CNP, sex="F", birth_date=date(1980, 3, 20), demo_source="dicom")
    assert demographics.reconcile(pat, dicom_sex="F", dicom_birth=date(1980, 3, 20)) is True
    assert (pat.sex, pat.birth_date, pat.demo_source) == ("M", date(1980, 3, 15), "cnp")
    assert "sex" in pat.demo_conflict


def test_reconcile_cnp_of_another_person_ignored():
    # a baby registered under a parent's CNP: the header birth date is decades away
    pat = Patient(patient_id=CNP, demo_source="dicom")
    demographics.reconcile(pat, dicom_sex="F", dicom_birth=date(2024, 7, 2))
    assert (pat.sex, pat.birth_date, pat.demo_source) == ("F", date(2024, 7, 2), "dicom")
    assert "another person" in pat.demo_conflict


def test_reconcile_his_over_dicom():
    pat = Patient(patient_id="PAT-002", sex="M", birth_date=date(1990, 4, 2), demo_source="dicom")
    changed = demographics.reconcile(pat, his_sex="F", his_birth=date(1985, 6, 15),
                                     dicom_sex="M", dicom_birth=date(1990, 4, 2))
    assert changed is True
    assert (pat.sex, pat.birth_date, pat.demo_source) == ("F", date(1985, 6, 15), "his")


def test_exact_age():
    st = Study(study_datetime=datetime(2026, 1, 15, 10, 0))
    assert demographics.exact_age(st, date(2010, 6, 1)) is True
    assert st.age_days == (date(2026, 1, 15) - date(2010, 6, 1)).days
    assert st.age_precision == AgePrecision.exact
    assert demographics.exact_age(st, date(2010, 6, 1)) is False  # unchanged
    assert demographics.exact_age(st, date(2027, 1, 1)) is False  # born after the study
    assert demographics.exact_age(st, None) is False


def test_refresh_ages(session):
    pat = Patient(patient_id="PAT-004", sex="M", birth_date=date(2010, 6, 1))
    session.add(pat)
    session.flush()
    st = Study(study_uid="1.2.4.99", patient_id=pat.id, modality="CT", source_kind="rdsr",
               study_datetime=datetime(2026, 1, 15, 10, 0))
    session.add(st)
    session.flush()
    assert demographics.refresh_ages(session, [pat.id]) == [st.id]
    assert st.age_precision == AgePrecision.exact
    assert demographics.refresh_ages(session, [pat.id]) == []
