"""Receiver end to end: a real DICOM association over localhost into a SQLite database."""

import socket

import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE
from pynetdicom.sop_class import Verification
from sqlalchemy import select

from dosetrail.db.migrate import upgrade
from dosetrail.db.models import Event, Study
from dosetrail.db.session import make_engine, make_sessionmaker
from dosetrail.net.scp import Receiver
from dosetrail.net.spool import Spool, SpoolWorker
from tests import dicom_factory as f


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def rx(tmp_path):
    url = f"sqlite:///{tmp_path / 'rx.sqlite3'}"
    upgrade(url)
    maker = make_sessionmaker(make_engine(url))
    spool = Spool(tmp_path / "spool")
    port = _free_port()
    receiver = Receiver(spool, "DOSETRAIL", port, "127.0.0.1", allowed_aets=["MODALITY"])
    receiver.start()
    worker = SpoolWorker(spool, maker, b"r" * 32)
    yield receiver, worker, spool, maker, port
    receiver.stop()


def _send(port, datasets, calling="MODALITY"):
    ae = AE(ae_title=calling)
    for ds in datasets:
        ae.add_requested_context(ds.SOPClassUID, ExplicitVRLittleEndian)
    ae.add_requested_context(Verification)
    assoc = ae.associate("127.0.0.1", port, ae_title="DOSETRAIL")
    if not assoc.is_established:
        return None
    statuses = [assoc.send_c_echo().Status]
    for ds in datasets:
        meta = FileMetaDataset()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.MediaStorageSOPClassUID = ds.SOPClassUID
        meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
        ds.file_meta = meta
        statuses.append(assoc.send_c_store(ds).Status)
    assoc.release()
    return statuses


def test_receive_and_import(rx):
    receiver, worker, spool, maker, port = rx
    ct = f.ct_rdsr(study_uid="1.2.9.1", sop_uid="1.2.9.1.1")
    dx = f.dx_image(study_uid="1.2.9.2", sop_uid="1.2.9.2.1")
    report = f.text_report(study_uid="1.2.9.3", sop_uid="1.2.9.3.1")
    assert _send(port, [ct, dx, report]) == [0, 0, 0, 0]
    assert len(spool.pending()) == 3 and receiver.received == 3
    assert worker.drain() == 3
    assert worker.stats == {"imported": 2, "ignored": 1}
    assert not spool.pending() and not list(spool.failed.iterdir())
    with maker() as s:
        st = s.scalar(select(Study).filter_by(study_uid="1.2.9.1"))
        assert st.n_events == 2 and st.total_dlp == pytest.approx(822.5)
        assert s.scalar(select(Event).filter_by(event_uid="1.2.9.2.1")).dap == pytest.approx(0.05)
    # the same objects again: stored by the receiver, recognised as duplicates
    assert _send(port, [ct])[1] == 0
    worker.drain()
    assert worker.stats["duplicate"] == 1


def test_unknown_calling_ae_is_rejected(rx):
    _, _, _, _, port = rx
    assert _send(port, [f.dx_image()], calling="INTRUDER") is None


def test_broken_object_goes_to_failed(rx):
    _, worker, spool, _, _ = rx
    spool.write("1.2.3.4", lambda p: p.write_bytes(b"not dicom at all"))
    worker.drain()
    failed = list(spool.failed.glob("*.dcm"))
    assert failed and failed[0].with_suffix(".reason").read_text().strip()
    assert worker.stats.get("failed") == 1


def test_two_workers_share_a_spool(rx):
    _, worker, spool, maker, port = rx
    other = SpoolWorker(spool, maker, b"r" * 32)
    objs = [f.dx_image(study_uid=f"1.2.8.{i}", sop_uid=f"1.2.8.{i}.1") for i in range(12)]
    assert _send(port, objs)[1:] == [0] * 12
    claimed = [p for p in spool.pending() if spool.claim(p)]  # claim everything once
    assert len(claimed) == 12 and spool.pending() == []
    assert all(spool.claim(p) is None for p in claimed)  # nobody can claim twice
    for w in spool.incoming.glob("*.work"):
        w.rename(w.with_suffix(".dcm"))
    import threading

    t = threading.Thread(target=other.drain)
    t.start()
    worker.drain()
    t.join()
    assert worker.stats.get("imported", 0) + other.stats.get("imported", 0) == 12
    assert not worker.stats.get("failed") and not other.stats.get("failed")
    assert not list(spool.incoming.iterdir())


def test_database_error_keeps_object_queued(rx, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from dosetrail.ingest import store as store_mod

    _, worker, spool, _, port = rx
    _send(port, [f.dx_image(study_uid="1.2.7.1", sop_uid="1.2.7.1.1")])

    def boom(self, obj):
        raise OperationalError("INSERT", {}, Exception("server has gone away"))

    monkeypatch.setattr(store_mod.Store, "store", boom)
    worker.drain()
    assert len(spool.pending()) == 1 and not list(spool.failed.iterdir())
    assert worker.stats["retried"] == 1
    monkeypatch.undo()
    worker.drain()
    assert worker.stats["imported"] == 1 and not spool.pending()


def test_forget_studies_for_refetch(session):
    import datetime as dt

    from sqlalchemy import func, select

    from dosetrail.db.models import AppSetting, Event, IngestedObject, Study
    from dosetrail.maintenance import forget_studies
    from dosetrail.net.qr import DONE_KEY

    s = Study(study_uid="f.1", modality="DX", source_kind="proj_rdsr",
              study_datetime=dt.datetime(2026, 9, 1, 10))
    s.events = [Event(kvp=70.0)]
    keep = Study(study_uid="f.2", modality="DX", source_kind="proj_rdsr",
                 study_datetime=dt.datetime(2026, 9, 2, 10), total_dap=0.1)
    session.add_all([s, keep])
    session.flush()
    session.add(IngestedObject(sop_instance_uid="f.1.1", study_id=s.id, status="imported",
                               source="qr"))
    session.add(AppSetting(key=DONE_KEY, value=["2026-09-01", "2026-09-02"]))
    session.flush()
    out = forget_studies(session, [s.id])
    assert out == {"studies": 1, "days": ["2026-09-01"], "objects": 1}
    assert session.scalar(select(func.count()).select_from(Study)) == 1
    assert session.scalar(select(func.count()).select_from(Event)) == 0
    assert session.get(AppSetting, DONE_KEY).value == ["2026-09-02"]


def test_concurrent_duplicate_is_retried_not_failed(rx, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from dosetrail.ingest import store as store_mod

    _, worker, spool, _, port = rx
    _send(port, [f.dx_image(study_uid="1.2.8.1", sop_uid="1.2.8.1.1")])

    def dup(self, obj):
        raise IntegrityError("INSERT", {}, Exception("Duplicate entry for key 'study_uid'"))

    monkeypatch.setattr(store_mod.Store, "store", dup)
    worker.drain()
    assert len(spool.pending()) == 1 and not list(spool.failed.iterdir())
    monkeypatch.undo()
    worker.drain()
    assert worker.stats["imported"] == 1 and not spool.pending()


def test_study_created_by_another_session_is_reused(tmp_path):
    from sqlalchemy import select

    from dosetrail.db.migrate import upgrade
    from dosetrail.db.models import Study
    from dosetrail.db.session import make_engine, make_sessionmaker
    from dosetrail.ingest.parse import parse
    from dosetrail.ingest.store import Store

    url = f"sqlite:///{tmp_path / 'race.sqlite3'}"
    upgrade(url)
    maker = make_sessionmaker(make_engine(url))
    with maker() as other:  # another worker committed the study a moment ago
        other.add(Study(study_uid="1.2.9.1", modality="DX", source_kind="header", n_events=0))
        other.commit()
    obj = parse(f.dx_image(study_uid="1.2.9.1", sop_uid="1.2.9.1.1"))
    with maker() as s1:
        store = Store(s1, b"k" * 32)
        real = s1.scalar
        missed = {"done": False}

        def lookup(stmt):
            # ... but this worker's first lookup ran before that commit and missed it
            if not missed["done"] and "FROM study" in str(stmt) and "FOR UPDATE" not in \
                    str(stmt.compile()).upper():
                missed["done"] = True
                return None
            return real(stmt)

        s1.scalar = lookup
        r = store.store(obj)
        s1.scalar = real
        s1.commit()
        assert missed["done"] and r.status == "imported"
        assert len(list(s1.scalars(select(Study).filter_by(study_uid="1.2.9.1")))) == 1
