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
