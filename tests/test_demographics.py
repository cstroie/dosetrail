"""Tests for demographics.py: patient sex/birth date precedence."""

from __future__ import annotations

from datetime import date, datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dozimetrix.db.session import make_sessionmaker
from dozimetrix.db.models import Base, Patient, Study, AgePrecision
from dozimetrix import demographics


class TestDemographics:
    def setup_method(self):
        engine = create_engine("sqlite:///:memory:")
        maker = make_sessionmaker(engine)
        with maker() as s:
            Base.metadata.create_all(engine)
        self.maker = maker

    def test_age_precision_enums(self):
        """AgePrecision enum values."""
        assert AgePrecision.DAYS.value == "days"
        assert AgePrecision.MONTHS.value == "months"
        assert AgePrecision.YEARS.value == "years"
        assert AgePrecision.EXACT.value == "exact"

    def test_norm_sex(self):
        from dozimetrix.demographics import _norm_sex
        assert _norm_sex("M") == "M"
        assert _norm_sex("m") == "M"
        assert _norm_sex("F") == "F"
        assert _norm_sex("f") == "F"
        assert _norm_sex("O") == "O"
        assert _norm_sex("X") is None
        assert _norm_sex("") is None
        assert _norm_sex(None) is None

    def test_real_birth_date(self):
        from dozimetrix.demographics import _real
        assert _real(date(1980, 1, 1)) is True
        assert _real(date(2000, 6, 15)) is True
        assert _real(date(1900, 1, 1)) is False  # before 1901
        assert _real(date(1901, 1, 1)) is True
        assert _real(date(2015, 1, 1)) is False  # 1 January placeholder
        assert _real(None) is False

    def test_foreign_birth_date(self):
        from dozimetrix.demographics import _foreign
        headers = [date(2020, 1, 1), date(2021, 6, 15)]
        # Birth date far from any header -> someone else
        assert _foreign(date(1980, 1, 1), headers) == date(2020, 1, 1)
        # Birth date close to header -> not someone else
        assert _foreign(date(2020, 2, 1), headers) is None
        assert _foreign(None, headers) is None

    def test_another_person(self):
        with self.maker() as s:
            patient = Patient(
                id=1,
                patient_id="PAT-001",
                sex="M",
                birth_date=date(2020, 1, 1),
            )
            s.add(patient)
            s.commit()
        
        with self.maker() as s:
            patient = s.get(Patient, 1)
            # HIS record with very different birth date
            assert demographics.another_person(patient, date(1980, 1, 1)) is True
            # HIS record with similar birth date
            assert demographics.another_person(patient, date(2020, 2, 1)) is False

    def test_reconcile_cnp_priority(self):
        """CNP (national ID) has highest priority when valid."""
        with self.maker() as s:
            patient = Patient(
                id=2,
                patient_id="1800101123456",  # valid CNP: male, born 1980-01-01
                sex="F",  # stored from DICOM
                birth_date=date(1990, 1, 1),  # stored from DICOM
                demo_source="dicom",
                demo_conflict="",
            )
            s.add(patient)
            s.commit()
        
        with self.maker() as s:
            patient = s.get(Patient, 2)
            changed = demographics.reconcile(patient, dicom_sex="F", dicom_birth=date(1990, 1, 1))
            assert changed is True
            # CNP should win: male, 1980-01-01
            assert patient.sex == "M"
            assert patient.birth_date == date(1980, 1, 1)
            assert patient.demo_source == "cnp"

    def test_reconcile_his_over_dicom(self):
        """HIS takes precedence over DICOM when no CNP."""
        with self.maker() as s:
            patient = Patient(
                id=3,
                patient_id="PAT-002",  # not a CNP
                sex="M",  # stored from DICOM
                birth_date=date(1990, 1, 1),  # stored from DICOM
                demo_source="dicom",
                demo_conflict="",
            )
            s.add(patient)
            s.commit()
        
        with self.maker() as s:
            patient = s.get(Patient, 3)
            changed = demographics.reconcile(
                patient, 
                his_sex="F", 
                his_birth=date(1985, 6, 15),
                dicom_sex="M",
                dicom_birth=date(1990, 1, 1),
            )
            assert changed is True
            # HIS should win
            assert patient.sex == "F"
            assert patient.birth_date == date(1985, 6, 15)
            assert patient.demo_source == "his"

    def test_exact_age(self):
        """Study exact age computed from birth date."""
        with self.maker() as s:
            patient = Patient(
                id=4,
                patient_id="PAT-003",
                sex="M",
                birth_date=date(2010, 6, 1),
            )
            study = Study(
                id=10,
                study_uid="1.2.3",
                patient_id=4,
                modality="CT",
                study_datetime=datetime(2026, 1, 15, 10, 0),
                study_description="CT Head",
            )
            s.add_all([patient, study])
            s.commit()
        
        with self.maker() as s:
            study = s.get(Study, 10)
            patient = s.get(Patient, 4)
            changed = demographics.exact_age(study, patient.birth_date)
            assert changed is True
            assert study.age_days == (date(2026, 1, 15) - date(2010, 6, 1)).days
            assert study.age_precision == AgePrecision.EXACT

    def test_refresh_ages(self):
        """Refresh ages for multiple patients."""
        with self.maker() as s:
            patient = Patient(
                id=5,
                patient_id="PAT-004",
                sex="M",
                birth_date=date(2010, 6, 1),
            )
            study = Study(
                id=11,
                study_uid="1.2.4",
                patient_id=5,
                modality="CT",
                study_datetime=datetime(2026, 1, 15, 10, 0),
                study_description="CT Head",
            )
            s.add_all([patient, study])
            s.commit()
        
        with self.maker() as s:
            changed_ids = demographics.refresh_ages(s, [5])
            assert 11 in changed_ids


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
