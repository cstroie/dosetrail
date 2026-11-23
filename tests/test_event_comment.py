"""An event comment longer than 255 characters (some devices write 300+) must be stored."""

import datetime as dt

from sqlalchemy import select

from dosimetrix.db.models import Device, Event, Patient, Study


def test_long_event_comment_is_stored(session):
    dev = Device(display_name="X", identity_key="ec-1", is_local=True)
    pat = Patient(pseudo_id="P-EC1", sex="M")
    session.add_all([dev, pat])
    session.flush()
    s = Study(study_uid="ec.1", device_id=dev.id, patient_id=pat.id, source_kind="test", modality="DX",
              study_datetime=dt.datetime(2026, 8, 18, 8, 30))
    session.add(s)
    session.flush()
    text = "dose comment " * 40  # 520 characters
    session.add(Event(study_id=s.id, event_uid="ec.1.e", comment=text))
    session.flush()
    session.expire_all()
    assert session.scalar(select(Event.comment).where(Event.study_id == s.id)) == text
