"""Query/retrieve against a fake PACS (pynetdicom C-FIND + C-GET SCP) with synthetic data."""

import datetime as dt
import socket

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind as FIND,
)
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelGet as GET,
)
from sqlalchemy import select

from dosetrail.db.migrate import upgrade
from dosetrail.db.models import Study
from dosetrail.db.session import make_engine, make_sessionmaker
from dosetrail.net import qr
from dosetrail.net.spool import Spool, SpoolWorker
from tests import dicom_factory as f

DAY = dt.date(2025, 11, 6)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _with_meta(ds):
    m = FileMetaDataset()
    m.TransferSyntaxUID = ExplicitVRLittleEndian
    m.MediaStorageSOPClassUID, m.MediaStorageSOPInstanceUID = ds.SOPClassUID, ds.SOPInstanceUID
    ds.file_meta = m
    return ds


class FakePACS:
    """Holds objects grouped as study -> series -> instances."""

    def __init__(self, objects):
        self.objects = [_with_meta(o) for o in objects]
        self.gets: list[str] = []
        self.ae = AE(ae_title="FAKEPACS")
        self.ae.add_supported_context(FIND)
        self.ae.add_supported_context(GET)
        for uid in qr.SR_CLASSES + qr.IMAGE_CLASSES:
            self.ae.add_supported_context(uid, ExplicitVRLittleEndian, scu_role=True,
                                          scp_role=True)
        self.port = _free_port()
        self.server = self.ae.start_server(
            ("127.0.0.1", self.port), block=False,
            evt_handlers=[(evt.EVT_C_FIND, self.on_find), (evt.EVT_C_GET, self.on_get)])

    def on_find(self, event):
        q = event.identifier
        if q.QueryRetrieveLevel == "STUDY":
            studies = {}
            for o in self.objects:
                if o.StudyDate != q.StudyDate:
                    continue
                studies.setdefault(o.StudyInstanceUID, set()).add(o.Modality)
            for uid, mods in studies.items():
                r = Dataset()
                r.QueryRetrieveLevel = "STUDY"
                r.StudyInstanceUID = uid
                r.ModalitiesInStudy = sorted(mods)
                yield 0xFF00, r
        else:
            series = {}
            for o in self.objects:
                if o.StudyInstanceUID == q.StudyInstanceUID:
                    series.setdefault(o.SeriesInstanceUID, [o.Modality, 0])[1] += 1
            for uid, (mod, n) in series.items():
                r = Dataset()
                r.QueryRetrieveLevel = "SERIES"
                r.StudyInstanceUID, r.SeriesInstanceUID = q.StudyInstanceUID, uid
                r.Modality, r.NumberOfSeriesRelatedInstances = mod, n
                yield 0xFF00, r

    def on_get(self, event):
        q = event.identifier
        hits = [o for o in self.objects if o.SeriesInstanceUID == q.SeriesInstanceUID]
        self.gets.append(q.SeriesInstanceUID)
        yield len(hits)
        for o in hits:
            yield 0xFF00, o

    def stop(self):
        self.server.shutdown()


def _date(ds, d="20251106"):
    ds.StudyDate = d
    return ds


@pytest.fixture
def env(tmp_path):
    url = f"sqlite:///{tmp_path / 'qr.sqlite3'}"
    upgrade(url)
    maker = make_sessionmaker(make_engine(url))
    spool = Spool(tmp_path / "spool")
    worker = SpoolWorker(spool, maker, b"q" * 32)

    def drain():
        total = 0
        while n := worker.drain():
            total += n
        return total

    return maker, spool, worker, drain


def test_backfill_day(env):
    maker, spool, worker, drain = env
    ct_sr = _date(f.ct_rdsr(study_uid="5.1", sop_uid="5.1.1"))
    dx = _date(f.dx_image(study_uid="5.2", sop_uid="5.2.1"))
    rf_sr = _date(f.proj_rdsr(study_uid="5.3", sop_uid="5.3.1"))
    rf_img = _date(f.dx_image(study_uid="5.3", sop_uid="5.3.2"))  # covered by the RDSR
    us = _date(f.dx_image(study_uid="5.4", sop_uid="5.4.1"))
    us.Modality = "US"
    pacs = FakePACS([ct_sr, dx, rf_sr, rf_img, us])
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOSETRAIL", timeout=10)
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, pause=0, progress=lambda _m: None,
                   workers=3)
        c = stats.counts
        assert c["skipped_not_xray"] == 1 and c["studies_done"] == 3
        assert c["studies_by_dose_report"] == 2 and c["studies_by_headers"] == 1
        assert "5.3.2.9" not in pacs.gets  # images of an RDSR-covered study not fetched
        with maker() as s:
            uids = {x.study_uid: x for x in s.scalars(select(Study))}
        assert set(uids) == {"5.1", "5.2", "5.3"}
        assert uids["5.1"].total_dlp == pytest.approx(822.5)
        assert uids["5.2"].total_dap == pytest.approx(0.05)

        # a second run skips what is already in the database
        stats2 = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats2, pause=0, progress=lambda _m: None)
        assert stats2.counts["skipped_known"] == 3 and not stats2.counts["studies_done"]
    finally:
        pacs.stop()


def test_dry_run_and_done_days(env):
    maker, spool, worker, drain = env
    pacs = FakePACS([_date(f.dx_image(study_uid="6.1", sop_uid="6.1.1"))])
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", timeout=10)
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, dry_run=True, pause=0,
                   progress=lambda _m: None)
        assert stats.counts["would_retrieve"] == 1 and not pacs.gets
    finally:
        pacs.stop()
    with maker() as s:
        qr.mark_day_done(s, DAY)
        qr.mark_day_done(s, DAY)
        assert qr.done_days(s) == {"2025-11-06"}


def test_partial_modality_run_does_not_finish_the_day(tmp_path, monkeypatch):
    from dosetrail import cli

    calls = []
    monkeypatch.setattr(qr, "run_day", lambda *a, **k: calls.append(a[6]))  # mods
    url = f"sqlite:///{tmp_path / 'p.sqlite3'}"
    upgrade(url)
    monkeypatch.setenv("DOSETRAIL_SECRET_FILE", str(tmp_path / "secret"))
    monkeypatch.setenv("DOSETRAIL_CONFIG", str(tmp_path / "none.toml"))
    base = ["qr", "--url", url, "--host", "h", "--aec", "P", "--from", "2025-11-06",
            "--to", "2025-11-06"]
    assert cli.main(base + ["--modalities", "CT"]) == 0
    assert cli.main(base) == 0  # still runs: the CT-only run did not finish the day
    assert calls == [{"CT"}, None]
    with make_sessionmaker(make_engine(url))() as s:
        assert qr.done_days(s) == {"2025-11-06"}
