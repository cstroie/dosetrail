"""maintenance.py: recomputations and repairs run by Admin → Maintenance and the scheduler."""

from __future__ import annotations

from datetime import datetime

from dozimetrix import maintenance
from dozimetrix.db.migrate import upgrade
from dozimetrix.db.models import Device, Event, IngestedObject, Study
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.ingest.store import DERIVE_FAILED


def _study(session, uid, modality="CT", device=None, when=None):
    st = Study(study_uid=uid, modality=modality, source_kind="rdsr",
               device_id=device.id if device else None,
               study_datetime=when or datetime(2026, 1, 15, 10, 0))
    session.add(st)
    session.flush()
    return st


def _refresh(session, obj):
    session.expire_all()
    return session.get(type(obj), obj.id)


def test_recompute_ct_columns(session):
    st = _study(session, "1.2.3.1")
    session.add_all([
        Event(study_id=st.id, event_type="spiral", ctdivol=15, dlp=500),
        Event(study_id=st.id, event_type="axial", ctdivol=10, dlp=200),
        Event(study_id=st.id, event_type="spiral", ctdivol=99, is_localizer=True),
    ])
    session.flush()
    assert maintenance.recompute_ct_columns(session, [st.id]) == 1
    st = _refresh(session, st)
    assert (st.n_spiral, st.n_axial, st.n_localizer, st.ctdivol_max) == (2, 1, 1, 15)


def test_relabel_hybrid(session):
    pet = _study(session, "1.2.3.2")
    session.add_all([
        Event(study_id=pet.id, event_type="spiral", dlp=500),
        Event(study_id=pet.id, event_type="radiopharmaceutical", extra_json={"half_life_s": 6586}),
        IngestedObject(sop_instance_uid="1.2.3.2.1", study_id=pet.id, source="file",
                       status="imported", sop_class_uid="1.2.840.10008.5.1.4.1.1.88.67"),
    ])
    spect = _study(session, "1.2.3.3")
    session.add(Event(study_id=spect.id, event_type="radiopharmaceutical",
                      extra_json={"half_life_s": 21624}))
    plain = _study(session, "1.2.3.4")
    session.flush()
    assert maintenance.relabel_hybrid(session) == 2
    assert _refresh(session, pet).modality == "PT"  # F-18: short half-life
    assert _refresh(session, spect).modality == "NM"  # Tc-99m
    assert _refresh(session, plain).modality == "CT"


def test_recompute_device_stats(session):
    dev = Device(identity_key="test-dev-stats", manufacturer="T", model_name="M")
    session.add(dev)
    session.flush()
    _study(session, "1.2.3.5", device=dev, when=datetime(2026, 1, 1, 8, 0))
    _study(session, "1.2.3.6", device=dev, when=datetime(2026, 2, 1, 8, 0))
    maintenance.recompute_device_stats(session)
    dev = _refresh(session, dev)
    assert dev.n_studies == 2
    assert dev.first_study_at == datetime(2026, 1, 1, 8, 0)
    assert dev.last_study_at == datetime(2026, 2, 1, 8, 0)


def test_zero_doses_as_missing(session):
    st = _study(session, "1.2.3.7", modality="DX")
    session.add_all([Event(study_id=st.id, event_type="projection", dap=0.0),
                     Event(study_id=st.id, event_type="projection", dap=1.5)])
    session.flush()
    assert maintenance.zero_doses_as_missing(session) == 1
    zero = session.query(Event).filter_by(study_id=st.id, dap=None).one()
    assert zero.extra_json == {"reported_zero": ["dap"]}
    assert _refresh(session, st).total_dap == 1.5


def test_repair_failed_derive_and_status(session):
    st = _study(session, "1.2.3.8")
    session.add_all([
        IngestedObject(sop_instance_uid="1.2.3.8.1", study_id=st.id, source="file",
                       status="imported", message=f"{DERIVE_FAILED} (try 1)"),
        IngestedObject(sop_instance_uid="1.2.3.8.2", study_id=None, source="file",
                       status="imported", message=f"{DERIVE_FAILED} (gave up)"),
    ])
    session.flush()
    before = maintenance.derive_status(session)
    assert before["derive_pending"] >= 1 and before["derive_gave_up"] == 0  # null study

    out = maintenance.repair_failed_derive(session)
    assert out["retried"] >= 1 and out["repaired"] >= 1
    obj = session.query(IngestedObject).filter_by(sop_instance_uid="1.2.3.8.1").one()
    assert obj.message == "derived on retry"


def test_run_periodic(tmp_path):
    url = f"sqlite:///{tmp_path / 'periodic.sqlite3'}"
    upgrade(url)
    engine = make_engine(url)
    try:
        maker = make_sessionmaker(engine)
        with maker() as s:
            _study(s, "1.2.3.9", when=datetime.now())
            s.commit()
        result = maintenance.run_periodic(maker, days=90)
        assert result["mode"] == "full"  # first run: no rules fingerprint stored yet
        assert result["tasks"] and all(t["ok"] for t in result["tasks"].values()), result
        assert maintenance.run_periodic(maker, days=90)["mode"] == "last 90 days"
    finally:
        engine.dispose()
