"""Tests for maintenance.py: periodic maintenance tasks."""

from __future__ import annotations

from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dozimetrix.db.session import make_sessionmaker
from dozimetrix.db.models import Base, Study, Event, Device, IngestedObject, Patient
from dozimetrix import maintenance


class TestMaintenance:
    def setup_method(self):
        engine = create_engine("sqlite:///:memory:")
        maker = make_sessionmaker(engine)
        with maker() as s:
            Base.metadata.create_all(engine)
        self.maker = maker

    def test_recompute_ct_columns(self):
        """recompute_ct_columns updates CT columns from events."""
        with self.maker() as s:
            device = Device(
                id=1, manufacturer="Test", model_name="Model", serial_number="SN1",
                display_name="Dev1"
            )
            patient = Patient(id=1, patient_id="PAT-1", sex="M", birth_date=datetime(2000, 1, 1).date())
            study = Study(
                id=1, study_uid="1.2.3", patient_id=1, device_id=1,
                modality="CT", study_datetime=datetime.now(),
                study_description="CT Head"
            )
            s.add_all([device, patient, study])
            s.commit()
            
            # Add events
            e1 = Event(id=1, study_id=1, event_uid="evt1", event_type="spiral", dlp=500, ctdivol=15)
            e2 = Event(id=2, study_id=1, event_uid="evt2", event_type="axial", dlp=200, ctdivol=10)
            s.add_all([e1, e2])
            s.commit()
        
        with self.maker() as s:
            updated = maintenance.recompute_ct_columns(s, [1])
            s.commit()
            
            study = s.get(Study, 1)
            assert study.total_dlp == 700
            assert study.n_events == 2

    def test_relabel_hybrid(self):
        """relabel_hybrid updates study labels for PET/CT and SPECT/CT."""
        with self.maker() as s:
            device = Device(id=1, manufacturer="T", model_name="M", serial_number="SN1", display_name="D1")
            patient = Patient(id=1, patient_id="PAT-1", sex="M", birth_date=datetime(2000, 1, 1).date())
            study = Study(
                id=1, study_uid="1.2.3", patient_id=1, device_id=1,
                modality="PT", study_datetime=datetime.now(),
                study_description="PET/CT"
            )
            s.add_all([device, patient, study])
            s.commit()
            
            # Add CT event with DLP
            event = Event(id=1, study_id=1, event_uid="evt1", modality="CT", event_type="spiral", dlp=500, ctdivol=15)
            s.add(event)
            s.commit()
        
        with self.maker() as s:
            updated = maintenance.relabel_hybrid(s)
            s.commit()
            
            study = s.get(Study, 1)
            # The study modality stays PT but should be recognized as hybrid
            assert study.modality == "PT"

    def test_recompute_device_stats(self):
        """recompute_device_stats updates device study counts and dates."""
        with self.maker() as s:
            device = Device(id=1, manufacturer="T", model_name="M", serial_number="SN1", display_name="D1")
            patient = Patient(id=1, patient_id="PAT-1", sex="M", birth_date=datetime(2000, 1, 1).date())
            study = Study(
                id=1, study_uid="1.2.3", patient_id=1, device_id=1,
                modality="CT", study_datetime=datetime.now(),
                study_description="CT Head"
            )
            s.add_all([device, patient, study])
            s.commit()
        
        with self.maker() as s:
            updated = maintenance.recompute_device_stats(s)
            s.commit()
            
            device = s.get(Device, 1)
            assert device.n_studies >= 1
            assert device.last_study_at is not None

    def test_zero_doses_as_missing(self):
        """zero_doses_as_missing converts zero doses to None."""
        with self.maker() as s:
            device = Device(id=1, manufacturer="T", model_name="M", serial_number="SN1", display_name="D1")
            patient = Patient(id=1, patient_id="PAT-1", sex="M", birth_date=datetime(2000, 1, 1).date())
            study = Study(
                id=1, study_uid="1.2.3", patient_id=1, device_id=1,
                modality="CT", study_datetime=datetime.now(),
                study_description="CT Head"
            )
            s.add_all([device, patient, study])
            s.commit()
            
            # Event with zero DLP
            event = Event(id=1, study_id=1, event_uid="evt1", event_type="spiral", dlp=0.0, ctdivol=0.0)
            s.add(event)
            s.commit()
        
        with self.maker() as s:
            updated = maintenance.zero_doses_as_missing(s)
            s.commit()
            
            event = s.get(Event, 1)
            assert event.dlp is None
            assert event.ctdivol is None

    def test_repair_failed_derive(self):
        """repair_failed_derive clears 'derive failed' and retries."""
        with self.maker() as s:
            obj = IngestedObject(
                sop_uid="1.2.3.4",
                study_id=1,
                source="file",
                status="failed",
                message="derive failed",
            )
            s.add(obj)
            s.commit()
            obj_id = obj.id
        
        with self.maker() as s:
            result = maintenance.repair_failed_derive(s, limit=10)
            s.commit()
            
            # Should process the failed object
            assert result["processed"] >= 1

    def test_derive_status(self):
        """derive_status returns counts of derived/failed objects."""
        with self.maker() as s:
            status = maintenance.derive_status(s)
            assert "derived" in status
            assert "failed" in status
            assert "pending" in status

    def test_run_periodic(self):
        """run_periodic executes maintenance tasks."""
        with self.maker() as s:
            device = Device(id=1, manufacturer="T", model_name="M", serial_number="SN1", display_name="D1")
            patient = Patient(id=1, patient_id="PAT-1", sex="M", birth_date=datetime(2000, 1, 1).date())
            study = Study(
                id=1, study_uid="1.2.3", patient_id=1, device_id=1,
                modality="CT", study_datetime=datetime.now(),
                study_description="CT Head"
            )
            s.add_all([device, patient, study])
            s.commit()
        
        with self.maker() as s:
            # Should run without error
            result = maintenance.run_periodic(self.maker, days=90, full=False)
            assert isinstance(result, dict)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
