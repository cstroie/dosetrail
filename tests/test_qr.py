"""Query/retrieve against a fake PACS (pynetdicom C-FIND + C-GET SCP) with synthetic data."""

import datetime as dt
import socket

import pytest

from dozimetrix.ingest.parse import parse as parse_object
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelFind as FIND,
)
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelGet as GET,
)
from pynetdicom.sop_class import (
    StudyRootQueryRetrieveInformationModelMove as MOVE,
)
from sqlalchemy import select

from dozimetrix.db.migrate import upgrade
from dozimetrix.db.models import Study
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.net import qr
from dozimetrix.net.spool import Spool, SpoolWorker
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

    def __init__(self, objects, cap=None, move_to=None):
        self.objects = [_with_meta(o) for o in objects]
        self.gets: list[str] = []
        self.finds: list[tuple] = []
        self.cap, self.move_to = cap, move_to  # move_to: {aet: (host, port)}
        self.ae = AE(ae_title="FAKEPACS")
        self.ae.add_supported_context(FIND)
        self.ae.add_supported_context(GET)
        self.ae.add_supported_context(MOVE)
        for uid in qr.SR_CLASSES + qr.IMAGE_CLASSES:
            self.ae.add_requested_context(uid, ExplicitVRLittleEndian)
        for uid in qr.SR_CLASSES + qr.IMAGE_CLASSES:
            self.ae.add_supported_context(uid, ExplicitVRLittleEndian, scu_role=True,
                                          scp_role=True)
        self.port = _free_port()
        self.server = self.ae.start_server(
            ("127.0.0.1", self.port), block=False,
            evt_handlers=[(evt.EVT_C_FIND, self.on_find), (evt.EVT_C_GET, self.on_get),
                          (evt.EVT_C_MOVE, self.on_move)])

    def on_find(self, event):
        q = event.identifier
        if q.QueryRetrieveLevel == "STUDY":
            want = str(q.get("ModalitiesInStudy", "") or "")
            t = str(q.get("StudyTime", "") or "")
            lo, hi = (t.split("-") + ["235959"])[:2] if t else ("000000", "235959")
            self.finds.append((want, t))
            studies = {}
            for o in self.objects:
                if o.StudyDate != q.StudyDate or not (lo <= o.StudyTime <= hi):
                    continue
                studies.setdefault(o.StudyInstanceUID, set()).add(o.Modality)
            if want:
                studies = {u: m for u, m in studies.items() if want in m}
            for n, (uid, mods) in enumerate(studies.items()):
                if self.cap and n >= self.cap:
                    break
                r = Dataset()
                r.QueryRetrieveLevel = "STUDY"
                r.StudyInstanceUID = uid
                r.ModalitiesInStudy = sorted(mods)
                yield 0xFF00, r
        elif q.QueryRetrieveLevel == "IMAGE":
            for o in self.objects:
                if o.SeriesInstanceUID == q.SeriesInstanceUID:
                    r = Dataset()
                    r.QueryRetrieveLevel = "IMAGE"
                    r.SOPInstanceUID = o.SOPInstanceUID
                    r.InstanceNumber = o.get("InstanceNumber", 1)
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
        hits = [o for o in self.objects if o.SeriesInstanceUID == q.SeriesInstanceUID
                and (q.QueryRetrieveLevel != "IMAGE" or o.SOPInstanceUID == q.SOPInstanceUID)]
        self.gets.append(q.SeriesInstanceUID)
        yield len(hits)
        for o in hits:
            yield 0xFF00, o

    def on_move(self, event):
        q = event.identifier
        hits = [o for o in self.objects if o.SeriesInstanceUID == q.SeriesInstanceUID]
        dest = event.move_destination
        dest = dest.decode() if isinstance(dest, bytes) else str(dest)
        yield self.move_to[dest.strip()]
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
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOZIMETRIX", timeout=10)
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, pause=0, progress=lambda _m: None,
                   workers=3)
        c = stats.counts
        assert c["studies_done"] == 3  # the US study is never returned (per-modality query)
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
    from dozimetrix import cli

    calls = []
    monkeypatch.setattr(qr, "run_day", lambda *a, **k: calls.append(a[6]))  # mods
    url = f"sqlite:///{tmp_path / 'p.sqlite3'}"
    upgrade(url)
    cfg = tmp_path / "dozimetrix.toml"
    cfg.write_text(f'[security]\nsecret_file = "{tmp_path / "secret"}"\n')
    base = ["--config", str(cfg), "qr", "--url", url, "--host", "h", "--aec", "P",
            "--from", "2025-11-06",
            "--to", "2025-11-06"]
    assert cli.main(base + ["--modalities", "CT"]) == 0
    assert cli.main(base) == 0  # still runs: the CT-only run did not finish the day
    assert calls == [{"CT"}, {"CT", "DX", "CR", "RF", "XA", "MG", "NM", "PT", "SR"}]
    with make_sessionmaker(make_engine(url))() as s:
        assert qr.done_days(s) == {"2025-11-06"}


def test_queries_one_modality_at_a_time_and_splits_capped_days(env):
    maker, spool, worker, drain = env
    objs = []
    for i, hh in enumerate(["08", "09", "10", "13", "15", "17"]):
        o = _date(f.dx_image(study_uid=f"7.{i}", sop_uid=f"7.{i}.1"))
        o.StudyTime = f"{hh}0000"
        objs.append(o)
    pacs = FakePACS(objs, cap=2)
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", timeout=10, max_results=2)
        hits = qr.find_studies(node, DAY, "DX")
        assert sorted(h.uid for h in hits) == [f"7.{i}" for i in range(6)]
        assert all(m == "DX" for m, _ in pacs.finds) and len(pacs.finds) > 1
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, {"CT", "DX"}, pause=0,
                   progress=lambda _m: None)
        assert {m for m, _ in pacs.finds} == {"DX", "CT"}  # one query per modality
        assert stats.counts["studies_done"] == 6
    finally:
        pacs.stop()


def test_retrieve_with_c_move(env):
    from dozimetrix.net.scp import Receiver

    maker, spool, worker, drain = env
    rx_port = _free_port()
    receiver = Receiver(spool, "DOZIMETRIX", rx_port, "127.0.0.1")
    receiver.start()
    pacs = FakePACS([_date(f.ct_rdsr(study_uid="8.1", sop_uid="8.1.1"))],
                    move_to={"DOZIMETRIX": ("127.0.0.1", rx_port)})
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOZIMETRIX", timeout=10,
                       retrieve="move")
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, {"SR"}, pause=0,
                   progress=lambda _m: None)
        assert stats.counts["studies_by_dose_report"] == 1 and receiver.received == 1
        with maker() as s:
            assert s.scalar(select(Study).filter_by(study_uid="8.1")).n_events == 2
    finally:
        pacs.stop()
        receiver.stop()


def test_pet_ct_whose_images_came_first_gets_its_ct_dose_report(env):
    maker, spool, worker, drain = env
    pet = _date(f.pet_image(study_uid="5.9", sop_uid="5.9.1"))
    ct_sr = _date(f.ct_rdsr(study_uid="5.9", sop_uid="5.9.2"))
    from dozimetrix.ingest.parse import parse
    from dozimetrix.ingest.store import Store

    with maker() as s:  # the PET images were imported before the CT dose report existed
        Store(s, b"q" * 32).store(parse(pet))
        s.commit()
    with maker() as s:
        st = s.scalar(select(Study).filter_by(study_uid="5.9"))
        assert st.modality == "PT" and st.total_dlp is None
        assert qr.needs_ct_dose(s, "5.9")
    pacs = FakePACS([pet, ct_sr])
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOZIMETRIX", timeout=10)
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, pause=0, progress=lambda _m: None)
        assert stats.counts["ct_dose_studies"] == 1 and stats.counts["sr_objects"] == 1
        with maker() as s:
            st = s.scalar(select(Study).filter_by(study_uid="5.9"))
            assert st.modality == "PT" and st.total_dlp == pytest.approx(822.5)
            assert not qr.needs_ct_dose(s, "5.9")
        stats2 = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats2, pause=0, progress=lambda _m: None)
        assert stats2.counts["skipped_known"] == 1
        stats3 = qr.QRStats()  # the backfill finds nothing left to do
        qr.ct_dose_backfill(node, DAY, spool, drain, maker, stats3, pause=0,
                            progress=lambda _m: None)
        assert not stats3.counts
    finally:
        pacs.stop()


def test_ct_without_a_dose_report_falls_back_to_one_image_per_series(env):
    maker, spool, worker, drain = env
    imgs = [f.ct_series_image(study_uid="6.9", sop_uid=f"6.9.{i}", series_uid="6.9.s1",
                              ctdivol=10.0, count=None, spacing=5.0) for i in range(3)]
    imgs.append(f.ct_series_image(study_uid="6.9", sop_uid="6.9.9", series_uid="6.9.s2",
                                  ctdivol=4.0, count=None, spacing=3.0))
    for i, o in enumerate(imgs):
        o.InstanceNumber = i + 1
        o.ImagePositionPatient = [0, 0, -5.0 * i]  # 5 mm apart
        _date(o)
    pacs = FakePACS(imgs)
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOZIMETRIX", timeout=10)
        stats = qr.QRStats()
        qr.run_day(node, DAY, spool, drain, maker, stats, pause=0, progress=lambda _m: None)
        assert stats.counts["ct_header_images"] == 2  # one image per series, not all four
        assert len(pacs.gets) == 3  # + the last image of the 3-image series (its z position)
        with maker() as s:
            st = s.scalar(select(Study).filter_by(study_uid="6.9"))
            assert st.source_kind == "ct_header" and st.n_events == 2
            # series 1 (10 mGy): z range 10 mm + 5 mm slice + 0.7 x 20 mm beam over-ranging
            # = 29 mm; series 2 (4 mGy): one image, 1 x 3 mm
            assert st.total_dlp == pytest.approx(10.0 * 2.9 + 4.0 * 0.3)
            assert qr.needs_ct_sr(s, "6.9")
        stats2 = qr.QRStats()  # known only by headers: looked at again for a dose report
        qr.run_day(node, DAY, spool, drain, maker, stats2, pause=0, progress=lambda _m: None)
        assert stats2.counts["ct_dose_studies"] == 1 and stats2.counts["no_sr_series"] == 1
    finally:
        pacs.stop()
    sr = _date(f.ct_rdsr(study_uid="6.9", sop_uid="6.9.50"))
    pacs = FakePACS([*imgs, sr])
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOZIMETRIX", timeout=10)
        qr.run_day(node, DAY, spool, drain, maker, qr.QRStats(), pause=0,
                   progress=lambda _m: None)
        with maker() as s:
            st = s.scalar(select(Study).filter_by(study_uid="6.9"))
            assert st.source_kind == "ct_rdsr" and st.total_dlp == pytest.approx(822.5)
            assert not qr.needs_ct_sr(s, "6.9")
    finally:
        pacs.stop()


def test_a_day_with_undelivered_objects_is_not_complete():
    stats = qr.QRStats()
    stats.tally({"claimed": 3, "delivered": 3, "failed": 0})
    assert not stats.incomplete
    stats.tally({"claimed": 3, "delivered": 2, "failed": 0})  # the PACS announced more
    assert stats.incomplete and stats.counts["not_delivered"] == 1
    other = qr.QRStats()
    other.tally({"claimed": 2, "delivered": 2, "failed": 1})  # a failed sub-operation
    assert other.incomplete and other.counts["failed_suboperations"] == 1
    third = qr.QRStats()
    third.add("study_errors")
    assert third.incomplete


def test_old_header_estimates_are_improved_by_a_second_read(env):
    maker, spool, worker, drain = env
    imgs = [f.ct_series_image(study_uid="6.8", sop_uid=f"6.8.{i}", series_uid="6.8.s1",
                              ctdivol=10.0, count=None, spacing=5.0, acquisition="SEQUENCED",
                              pitch=None) for i in range(3)]
    for i, o in enumerate(imgs):
        o.InstanceNumber = i + 1
        o.ImagePositionPatient = [0, 0, -2.0 * i]  # 2 mm apart: the images overlap
        _date(o)
    pacs = FakePACS(imgs)
    try:
        node = qr.Node("127.0.0.1", pacs.port, "FAKEPACS", "DOZIMETRIX", timeout=10)
        # the study as an older version stored it: count x thickness (3 x 5 mm = 15 mm)
        old = f.ct_series_image(study_uid="6.8", sop_uid="6.8.0", series_uid="6.8.s1",
                                ctdivol=10.0, count=3, spacing=5.0, acquisition="SEQUENCED",
                                pitch=None)
        _date(old)
        parsed = parse_object(old)
        parsed.events[0]["extra_json"].pop("dlp_method")
        with maker() as s:
            from dozimetrix.ingest.store import Store
            Store(s, b"x" * 32).store(parsed)
            s.commit()
        stats = qr.QRStats()
        qr.ct_header_backfill(node, DAY, spool, drain, maker, stats, pause=0,
                              progress=lambda _m: None)
        assert stats.counts["ct_header_studies"] == 1
        with maker() as s:
            st = s.scalar(select(Study).filter_by(study_uid="6.8"))
            assert st.n_events == 1 and st.total_dlp == pytest.approx(10.0 * 0.9)  # 4 + 5 mm
        stats2 = qr.QRStats()  # nothing left to improve
        qr.ct_header_backfill(node, DAY, spool, drain, maker, stats2, pause=0,
                              progress=lambda _m: None)
        assert not stats2.counts
    finally:
        pacs.stop()
