"""Query/retrieve from a PACS: C-FIND (study, series) and C-GET into the spool.

Selection per study (OpenREM's proven order, adapted):
1. skip studies without X-ray modalities (US, MR…) and studies already in the database;
2. retrieve the SR series first and import them;
3. for projection radiography (DX/CR/MG/RF/XA) retrieve the image series only if no dose
   report covered the study – their headers carry the dose;
4. NM / PET: one image per series (its header holds the administered activity). A PET/CT
   or SPECT/CT whose images came first is revisited for its CT dose report (SR series
   only) until an SR of it has been seen;
5. CT: one image of the smallest series, only for its header – the operator and the
   performing physician, which most CT dose reports leave out. Other CT images never.

Queries go one day and one modality at a time: some PACS cap the number of studies a
C-FIND returns. When a query hits the configured cap (max_results), the day is split into
time windows and queried again.

Retrieval: C-GET (default, needs no PACS configuration) or C-MOVE to our AE title (the
PACS must know it; objects arrive at the receiver and are imported from the shared spool).
Push needs nothing here: modalities and the PACS simply send to the receiver.

Lessons from the field built in:
* every C-FIND response is consumed (breaking out early leaves the association waiting);
* the PACS may report a sub-operation "completed" although it could not deliver the object
  (no matching storage context), so we offer broad storage contexts and count what arrives;
* some PACS ignore ModalitiesInStudy as a filter, so modalities are filtered here.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from pydicom import dcmwrite
from pydicom.dataset import Dataset
from pydicom.uid import (
    DeflatedExplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
)
from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, build_role, evt
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
from sqlalchemy.orm import Session

from ..db.models import AppSetting, Device, Event, IngestedObject, Study
from ..ingest.parse import mark_series_instances, mark_series_last_z
from .spool import Spool

log = logging.getLogger("dosimetrix.qr")

XRAY = {"CT", "DX", "CR", "MG", "RF", "XA", "SR", "NM", "PT", "IO", "PX"}
# SR finds studies that hold only a dose report
DEFAULT_MODALITIES = ["CT", "DX", "CR", "RF", "XA", "MG", "NM", "PT", "SR"]
NM_IMAGES = {"NM", "PT"}
MAX_CT_SERIES = 12  # per study, for the header fallback
PROJECTION_IMAGES = {"DX", "CR", "MG", "RF", "XA"}
SR_CLASSES = [
    "1.2.840.10008.5.1.4.1.1.88.11", "1.2.840.10008.5.1.4.1.1.88.22",
    "1.2.840.10008.5.1.4.1.1.88.33", "1.2.840.10008.5.1.4.1.1.88.34",
    "1.2.840.10008.5.1.4.1.1.88.35", "1.2.840.10008.5.1.4.1.1.88.67",
    "1.2.840.10008.5.1.4.1.1.88.68", "1.2.840.10008.5.1.4.1.1.88.73",
]
IMAGE_CLASSES = [
    "1.2.840.10008.5.1.4.1.1.7",  # secondary capture (dose reports in disguise, screenshots)
    "1.2.840.10008.5.1.4.1.1.1", "1.2.840.10008.5.1.4.1.1.1.1", "1.2.840.10008.5.1.4.1.1.1.1.1",
    "1.2.840.10008.5.1.4.1.1.1.2", "1.2.840.10008.5.1.4.1.1.1.2.1",
    "1.2.840.10008.5.1.4.1.1.1.3", "1.2.840.10008.5.1.4.1.1.1.3.1",
    "1.2.840.10008.5.1.4.1.1.12.1", "1.2.840.10008.5.1.4.1.1.12.2",
    "1.2.840.10008.5.1.4.1.1.13.1.3",  # breast tomosynthesis
    "1.2.840.10008.5.1.4.1.1.2", "1.2.840.10008.5.1.4.1.1.2.1",  # CT (staff names only)
    "1.2.840.10008.5.1.4.1.1.20",  # NM
    "1.2.840.10008.5.1.4.1.1.128", "1.2.840.10008.5.1.4.1.1.128.1",
    "1.2.840.10008.5.1.4.1.1.130",  # PET
]
DONE_KEY = "qr.backfill.done_days"


@dataclass
class Node:
    host: str
    port: int
    aet: str  # the PACS
    calling_aet: str = "DOSIMETRIX"
    timeout: float = 60
    retrieve: str = "get"  # get | move
    move_destination: str | None = None  # default: calling_aet
    max_results: int | None = None  # the PACS's C-FIND cap, if known


@dataclass
class StudyHit:
    uid: str
    date: str
    modalities: set[str]
    description: str = ""
    instances: int | None = None


@dataclass
class QRStats:
    counts: Counter = field(default_factory=Counter)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] += n

    def tally(self, r: dict) -> None:
        """What a retrieval did not deliver: counted, and it keeps the day from being done."""
        if r["claimed"] > r["delivered"]:
            self.add("not_delivered", r["claimed"] - r["delivered"])
        if r.get("failed"):
            self.add("failed_suboperations", r["failed"])

    @property
    def incomplete(self) -> bool:
        """A study failed, or the PACS did not deliver everything it announced."""
        return any(self.counts.get(k) for k in
                   ("study_errors", "not_delivered", "failed_suboperations"))


# --------------------------------------------------------------------------- C-FIND


def _modalities(value) -> set[str]:
    if value is None:
        return set()
    items = value if isinstance(value, (list, tuple)) or value.__class__.__name__ == \
        "MultiValue" else str(value).split("\\")
    return {str(m).strip().upper() for m in items if str(m).strip()}


def _assoc_find(node: Node):
    ae = AE(ae_title=node.calling_aet)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = node.timeout
    ae.add_requested_context(FIND)
    assoc = ae.associate(node.host, node.port, ae_title=node.aet)
    if not assoc.is_established:
        raise ConnectionError(f"association with {node.aet}@{node.host}:{node.port} refused")
    return assoc


def _find(assoc, query: Dataset) -> list[Dataset]:
    out = []
    for status, ident in assoc.send_c_find(query, FIND):  # always consume every response
        if status and status.Status not in (0x0000, 0xFF00, 0xFF01):
            raise RuntimeError(f"C-FIND failed with status 0x{status.Status:04X}")
        if ident is not None:
            out.append(ident)
    return out


def _study_query(day: dt.date, modality: str | None, window: tuple[str, str] | None):
    q = Dataset()
    q.QueryRetrieveLevel = "STUDY"
    q.StudyDate = day.strftime("%Y%m%d")
    q.StudyTime = f"{window[0]}-{window[1]}" if window else ""
    q.StudyInstanceUID = ""
    q.ModalitiesInStudy = modality or ""
    q.StudyDescription = ""
    q.NumberOfStudyRelatedInstances = ""
    return q


def _split(window: tuple[str, str]) -> list[tuple[str, str]] | None:
    """Halve a HHMMSS window; None when it is already one hour or less."""
    def secs(t):
        return int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])

    def fmt(x):
        return f"{x // 3600:02d}{x % 3600 // 60:02d}{x % 60:02d}"

    a, b = secs(window[0]), secs(window[1])
    if b - a <= 3600:
        return None
    mid = (a + b) // 2
    return [(fmt(a), fmt(mid)), (fmt(mid + 1), fmt(b))]


def find_studies(node: Node, day: dt.date, modality: str | None = None,
                 warn: Callable[[str], None] = log.warning) -> list[StudyHit]:
    """Studies of one day (and one modality); splits the day if the PACS cap is reached."""
    assoc = _assoc_find(node)
    rows: dict[str, Dataset] = {}
    try:
        pending: list[tuple[str, str] | None] = [None]
        while pending:
            window = pending.pop()
            found = _find(assoc, _study_query(day, modality, window))
            if node.max_results and len(found) >= node.max_results:
                parts = _split(window or ("000000", "235959"))
                if parts:
                    pending.extend(parts)
                    continue
                warn(f"{day} {modality or ''} {window}: {len(found)} results, the PACS "
                     f"limit may hide studies")
            for r in found:
                uid = str(r.get("StudyInstanceUID", "")).strip()
                if uid:
                    rows[uid] = r
    finally:
        assoc.release()
    hits = []
    for uid, r in rows.items():
        n = r.get("NumberOfStudyRelatedInstances")
        hits.append(StudyHit(uid, day.strftime("%Y%m%d"), _modalities(r.get("ModalitiesInStudy")),
                             str(r.get("StudyDescription", "") or ""),
                             int(n) if n not in (None, "") else None))
    return hits


def find_series(node: Node, study_uid: str) -> list[dict]:
    q = Dataset()
    q.QueryRetrieveLevel = "SERIES"
    q.StudyInstanceUID = study_uid
    q.SeriesInstanceUID = ""
    q.Modality = ""
    q.SeriesDescription = ""
    q.NumberOfSeriesRelatedInstances = ""
    assoc = _assoc_find(node)
    try:
        rows = _find(assoc, q)
    finally:
        assoc.release()
    out = []
    for r in rows:
        n = r.get("NumberOfSeriesRelatedInstances")
        out.append({"uid": str(r.SeriesInstanceUID), "modality": str(r.get("Modality", "")),
                    "description": str(r.get("SeriesDescription", "") or ""),
                    "instances": int(n) if n not in (None, "") else None})
    return out


def first_instance(node: Node, study_uid: str, series_uid: str) -> str | None:
    """The SOP Instance UID of the series' lowest-numbered image."""
    return first_last_instance(node, study_uid, series_uid)[0]


def first_last_instance(node: Node, study_uid: str, series_uid: str):
    """SOP Instance UIDs of the series' lowest- and highest-numbered images."""
    q = Dataset()
    q.QueryRetrieveLevel = "IMAGE"
    q.StudyInstanceUID = study_uid
    q.SeriesInstanceUID = series_uid
    q.SOPInstanceUID = ""
    q.InstanceNumber = ""
    assoc = _assoc_find(node)
    try:
        rows = _find(assoc, q)
    finally:
        assoc.release()

    def number(r) -> int:
        try:
            return int(r.get("InstanceNumber"))
        except (TypeError, ValueError):
            return 1 << 30
    rows = [r for r in rows if r.get("SOPInstanceUID")]
    if not rows:
        return None, None
    return str(min(rows, key=number).SOPInstanceUID), str(max(rows, key=number).SOPInstanceUID)


def last_image_z(node: Node, study_uid: str, series_uid: str, sop: str) -> float | None:
    """Z position (mm) of one image, fetched into memory (not the spool)."""
    got: list[float] = []

    def on_store(event) -> int:
        pos = event.dataset.get("ImagePositionPatient")
        if pos is not None and len(pos) == 3:
            got.append(float(pos[2]))
        return 0x0000

    ae, roles = _get_ae(node)
    assoc = ae.associate(node.host, node.port, ae_title=node.aet, ext_neg=roles,
                         evt_handlers=[(evt.EVT_C_STORE, on_store)])
    if not assoc.is_established:
        raise ConnectionError("C-GET association refused")
    try:
        q = Dataset()
        _level(q, study_uid, series_uid, {series_uid: sop})
        for _status, _ident in assoc.send_c_get(q, GET):
            pass
    finally:
        assoc.release()
    return got[0] if got else None


# ---------------------------------------------------------------------------- C-GET


def _get_ae(node: Node) -> tuple[AE, list]:
    ae = AE(ae_title=node.calling_aet)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = node.timeout
    ae.maximum_pdu_size = 131072
    ae.add_requested_context(GET)
    roles = []
    for uid in SR_CLASSES:
        ae.add_requested_context(uid, [ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                                       DeflatedExplicitVRLittleEndian])
        roles.append(build_role(uid, scp_role=True))
    for uid in IMAGE_CLASSES:
        ae.add_requested_context(uid, ALL_TRANSFER_SYNTAXES)
        roles.append(build_role(uid, scp_role=True))
    return ae, roles


def _level(q: Dataset, study_uid: str, uid: str, instances: dict[str, str] | None) -> None:
    """Series level, or image level for a series given with one instance."""
    q.StudyInstanceUID = study_uid
    q.SeriesInstanceUID = uid
    if instances and uid in instances:
        q.QueryRetrieveLevel = "IMAGE"
        q.SOPInstanceUID = instances[uid]
    else:
        q.QueryRetrieveLevel = "SERIES"


def move_series(node: Node, study_uid: str, series_uids: list[str],
                instances: dict[str, str] | None = None) -> dict:
    """C-MOVE the series to our AE; the receiver writes them to the spool."""
    ae = AE(ae_title=node.calling_aet)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = node.timeout
    ae.add_requested_context(MOVE)
    assoc = ae.associate(node.host, node.port, ae_title=node.aet)
    if not assoc.is_established:
        raise ConnectionError("C-MOVE association refused")
    claimed = failed = 0
    try:
        for uid in series_uids:
            q = Dataset()
            _level(q, study_uid, uid, instances)
            last = None
            for status, _ident in assoc.send_c_move(q, node.move_destination
                                                    or node.calling_aet, MOVE):
                if status:
                    last = status
            if last is None:
                raise TimeoutError("no answer to C-MOVE")
            if last.Status not in (0x0000, 0xB000):
                raise RuntimeError(f"C-MOVE failed with status 0x{last.Status:04X}")
            claimed += int(last.get("NumberOfCompletedSubOperations", 0) or 0)
            failed += int(last.get("NumberOfFailedSubOperations", 0) or 0)
    finally:
        assoc.release()
    # the final C-MOVE response comes after every sub-operation; the receiver acknowledges
    # only once an object is in the spool, so everything is there now
    return {"delivered": claimed, "claimed": claimed, "failed": failed}


def retrieve(node: Node, study_uid: str, series_uids: list[str], spool: Spool,
             instances: dict[str, str] | None = None,
             counts: dict[str, int] | None = None,
             last_z: dict[str, float] | None = None) -> dict:
    """C-GET or C-MOVE, with one retry after a timeout or a dropped association.
    ``instances``: series UID -> the one SOP instance to fetch from it. ``counts``: series
    UID -> its image count, written into the object (C-GET only: C-MOVE objects go
    straight to the receiver, so their CT dose is estimated without a scan length)."""
    fn = (lambda: move_series(node, study_uid, series_uids, instances)) \
        if node.retrieve == "move" \
        else (lambda: get_series(node, study_uid, series_uids, spool, instances, counts, last_z))
    try:
        return fn()
    except (TimeoutError, ConnectionError, RuntimeError) as first:
        log.info("retrying %s after %s", study_uid, first)
        time.sleep(5)
        try:
            return fn()
        except (TimeoutError, ConnectionError, RuntimeError) as second:
            raise RuntimeError(f"retrieve failed twice ({first}; {second})") from second


def get_series(node: Node, study_uid: str, series_uids: list[str], spool: Spool,
               instances: dict[str, str] | None = None,
               counts: dict[str, int] | None = None,
               last_z: dict[str, float] | None = None) -> dict:
    """C-GET the series into the spool; returns delivered/claimed counts."""
    delivered = 0

    def on_store(event) -> int:
        nonlocal delivered
        uid = str(event.request.AffectedSOPInstanceUID)
        series = str(getattr(event.dataset, "SeriesInstanceUID", ""))
        n = (counts or {}).get(series)
        z = (last_z or {}).get(series)
        if n or z is not None:  # image count / last z of the series: the CT dose estimate
            ds = event.dataset
            ds.file_meta = event.file_meta
            if n:
                mark_series_instances(ds, n)
            if z is not None:
                mark_series_last_z(ds, z)
            spool.write(uid, lambda p: dcmwrite(p, ds, enforce_file_format=True))
        else:
            data = event.encoded_dataset()
            spool.write(uid, lambda p: p.write_bytes(data))
        delivered += 1
        return 0x0000

    ae, roles = _get_ae(node)
    assoc = ae.associate(node.host, node.port, ae_title=node.aet, ext_neg=roles,
                         evt_handlers=[(evt.EVT_C_STORE, on_store)])
    if not assoc.is_established:
        raise ConnectionError("C-GET association refused")
    claimed = failed = 0
    try:
        for uid in series_uids:
            q = Dataset()
            _level(q, study_uid, uid, instances)
            if not assoc.is_established:
                raise ConnectionError("the PACS dropped the association (timeout)")
            last = None
            for status, _ident in assoc.send_c_get(q, GET):
                if status:
                    last = status
            if last is None:
                raise TimeoutError("no answer to C-GET within the timeout")
            claimed += int(last.get("NumberOfCompletedSubOperations", 0) or 0)
            failed += int(last.get("NumberOfFailedSubOperations", 0) or 0)
    finally:
        assoc.release()
    return {"delivered": delivered, "claimed": claimed, "failed": failed}


# ------------------------------------------------------------------------- workflow


def in_database(session: Session, study_uid: str) -> bool:
    return session.scalar(select(Study.id).where(Study.study_uid == study_uid,
                                                  Study.n_events > 0)) is not None


def has_dose_report(session: Session, study_uid: str) -> bool:
    st = session.scalar(select(Study).where(Study.study_uid == study_uid))
    return bool(st and st.source_kind in ("ct_rdsr", "proj_rdsr") and st.n_events)


def needs_activity(session: Session, study_uid: str) -> bool:
    """A known study (e.g. the CT part of a PET/CT) with no radiopharmaceutical
    administration yet. One whose header gives no activity, or whose administration
    counts on another study, is not fetched again."""
    sid = session.scalar(select(Study.id).where(Study.study_uid == study_uid))
    return sid is not None and session.scalar(select(Event.id).where(
        Event.study_id == sid, Event.event_type == "radiopharmaceutical").limit(1)) is None


SR_CLASS = "1.2.840.10008.5.1.4.1.1.88."


def needs_ct_dose(session: Session, study_uid: str) -> bool:
    """A known NM / PET study without a CT dose, none of whose SR objects we have seen:
    the CT dose report of a PET/CT whose images came first."""
    sid = session.scalar(select(Study.id).where(
        Study.study_uid == study_uid, Study.modality.in_(NM_IMAGES), Study.total_dlp.is_(None)))
    return sid is not None and session.scalar(select(IngestedObject.id).where(
        IngestedObject.study_id == sid,
        IngestedObject.sop_class_uid.startswith(SR_CLASS)).limit(1)) is None


def needs_ct_sr(session: Session, study_uid: str) -> bool:
    """A CT study we know only from image headers (no dose report was available) and none
    of whose SR objects we have seen: look again, the dose report may have arrived."""
    sid = session.scalar(select(Study.id).where(
        Study.study_uid == study_uid, Study.modality == "CT", Study.source_kind == "ct_header"))
    return sid is not None and session.scalar(select(IngestedObject.id).where(
        IngestedObject.study_id == sid,
        IngestedObject.sop_class_uid.startswith(SR_CLASS)).limit(1)) is None


DOSE_PAGE_MAX_IMAGES = 5  # the console's dose screenshot is a series of a few pages


def needs_dose_page(session: Session, study_uid: str) -> bool:
    """A Toshiba CT study known only from image headers (no dose report exists): the dose
    pages the console sends as a small Secondary Capture series hold the scanner's own DLP."""
    return session.scalar(select(Study.id).join(Device, Device.id == Study.device_id).where(
        Study.study_uid == study_uid, Study.modality == "CT", Study.source_kind == "ct_header",
        Device.manufacturer.ilike("%toshiba%")).limit(1)) is not None


def fetch_dose_pages(node: Node, study_uid: str, spool: Spool, drain: Callable[[], int],
                     stats: QRStats, series: list[dict] | None = None) -> int:
    """Every image of the study's short CT series (the dose pages), whole."""
    series = find_series(node, study_uid) if series is None else series
    small = [s["uid"] for s in series if s["modality"] == "CT" and s["instances"]
             and s["instances"] <= DOSE_PAGE_MAX_IMAGES]
    if not small:
        stats.add("no_dose_page_series")
        return 0
    r = retrieve(node, study_uid, small, spool)
    stats.add("dose_page_objects", r["delivered"])
    stats.tally(r)
    if r["delivered"]:
        drain()
    return r["delivered"]


def dose_page_backfill(node: Node, day: dt.date, spool: Spool, drain: Callable[[], int],
                       session_factory, stats: QRStats, pause: float = 0.2,
                       progress: Callable[[str], None] = print) -> None:
    """Toshiba CT studies of the day already in the database from headers only: fetch
    their dose pages."""
    lo = dt.datetime.combine(day, dt.time())
    with session_factory() as session:
        uids = [u for u in session.scalars(
            select(Study.study_uid).where(Study.modality == "CT",
                                          Study.source_kind == "ct_header",
                                          Study.study_datetime >= lo,
                                          Study.study_datetime < lo + dt.timedelta(days=1)))
                if needs_dose_page(session, u)]
    if uids:
        progress(f"{day}: {len(uids)} Toshiba CT studies without a dose page")
    for uid in uids:
        try:
            fetch_dose_pages(node, uid, spool, drain, stats)
            stats.add("dose_page_studies")
        except Exception as e:  # keep going
            stats.add("study_errors")
            log.warning("dose pages of %s failed: %s", uid, e)
        time.sleep(pause)


def fetch_sr(node: Node, study_uid: str, spool: Spool, drain: Callable[[], int],
             stats: QRStats) -> int:
    """Only the study's SR series (the dose report)."""
    sr = [s["uid"] for s in find_series(node, study_uid) if s["modality"] == "SR"]
    if not sr:
        stats.add("no_sr_series")
        return 0
    r = retrieve(node, study_uid, sr, spool)
    stats.add("sr_objects", r["delivered"])
    stats.tally(r)
    if r["delivered"]:
        drain()
    return r["delivered"]


def needs_staff(session: Session, study_uid: str) -> bool:
    """A CT study we have whose operator and performing physician are both unknown."""
    return session.scalar(select(Study.id).where(
        Study.study_uid == study_uid, Study.modality == "CT",
        Study.operator_name.is_(None), Study.performing_physician.is_(None))) is not None


def one_per_series(node: Node, study_uid: str, series: list[dict], spool: Spool,
                   stats: QRStats, key: str, with_counts: bool = False) -> int:
    """Retrieve the first image of each series (its header is all we read). With
    ``with_counts`` (CT dose estimate) the object also records the series' image count and
    the z position of its last image (one more image fetched, kept in memory)."""
    instances: dict[str, str] = {}
    last_z: dict[str, float] = {}
    for s in series:
        first, last = first_last_instance(node, study_uid, s["uid"])
        if not first:
            continue
        instances[s["uid"]] = first
        if with_counts and last and last != first:
            try:
                z = last_image_z(node, study_uid, s["uid"], last)
            except (ConnectionError, TimeoutError, RuntimeError) as e:  # the count remains
                log.info("last image of %s: %s", s["uid"], e)
                z = None
            if z is not None:
                last_z[s["uid"]] = z
    if not instances:
        return 0
    counts = {s["uid"]: s["instances"] for s in series
              if with_counts and s["instances"] and s["uid"] in instances}
    r = retrieve(node, study_uid, list(instances), spool, instances, counts or None,
                 last_z or None)
    stats.add(key, r["delivered"])
    stats.tally(r)
    return r["delivered"]


def fetch_staff(node: Node, study_uid: str, series: list[dict], spool: Spool,
                drain: Callable[[], int], stats: QRStats) -> None:
    """One image of the smallest CT series: its header names the operator and physician."""
    ct = [s for s in series if s["modality"] == "CT"]
    if not ct:
        return
    ct.sort(key=lambda s: s["instances"] if s["instances"] else 1 << 30)
    if one_per_series(node, study_uid, ct[:1], spool, stats, "ct_staff_headers"):
        drain()


def retrieve_study(node: Node, hit: StudyHit, spool: Spool, drain: Callable[[], int],
                   session_factory, stats: QRStats, max_images: int = 50) -> None:
    series = find_series(node, hit.uid)
    sr = [s["uid"] for s in series if s["modality"] == "SR"]
    if sr:
        r = retrieve(node, hit.uid, sr, spool)
        stats.add("sr_objects", r["delivered"])
        stats.tally(r)
        drain()
    ct_headers = False
    if not sr and not any(s["modality"] in NM_IMAGES for s in series):
        # a CT study without a dose report: one image per series gives its dose header
        ct = [s for s in series if s["modality"] == "CT"][:MAX_CT_SERIES]
        if ct and one_per_series(node, hit.uid, ct, spool, stats, "ct_header_images",
                                 with_counts=True):
            drain()
            ct_headers = True
            stats.add("ct_studies_by_headers")
    nm = [s for s in series if s["modality"] in NM_IMAGES][:4]
    if nm and one_per_series(node, hit.uid, nm, spool, stats, "nm_headers"):
        drain()
    with session_factory() as session:
        covered = has_dose_report(session, hit.uid)
        staff = needs_staff(session, hit.uid)
        dose_page = ct_headers and needs_dose_page(session, hit.uid)
    if dose_page:  # a Toshiba without a dose SR: the console's own dose pages
        fetch_dose_pages(node, hit.uid, spool, drain, stats, series)
    if staff and not ct_headers:  # the CT headers carry the names as well
        fetch_staff(node, hit.uid, series, spool, drain, stats)
    if covered:
        stats.add("studies_by_dose_report")
        return
    images = [s for s in series if s["modality"] in PROJECTION_IMAGES
              and (s["instances"] is None or s["instances"] <= max_images)]
    if images:
        r = retrieve(node, hit.uid, [s["uid"] for s in images], spool)
        stats.add("image_objects", r["delivered"])
        stats.tally(r)
        drain()
        stats.add("studies_by_headers")
    elif not sr:
        stats.add("studies_without_dose_objects")


def run_day(node: Node, day: dt.date, spool: Spool, drain: Callable[[], int],
            session_factory, stats: QRStats, modalities: set[str] | None = None,
            dry_run: bool = False, pause: float = 0.2,
            progress: Callable[[str], None] = print, workers: int = 1) -> None:
    wanted = set(modalities) if modalities else set(DEFAULT_MODALITIES)
    by_uid: dict[str, StudyHit] = {}
    for mod in sorted(wanted):  # one modality at a time: the PACS caps result sizes
        for h in find_studies(node, day, mod):
            if h.uid in by_uid:
                by_uid[h.uid].modalities |= h.modalities
            else:
                by_uid[h.uid] = h
    hits = list(by_uid.values())
    todo, ct_dose = [], []
    with session_factory() as session:
        for h in hits:
            mods = h.modalities - {"SR", "PR", "KO", "OT", "DOC"}
            sr_only = bool(h.modalities) and h.modalities <= {"SR", "KO", "PR"}
            if not (mods & (wanted - {"SR"})) and not (sr_only and "SR" in wanted):
                stats.add("skipped_not_xray")
                continue
            if in_database(session, h.uid):
                if h.modalities & NM_IMAGES and needs_activity(session, h.uid):
                    todo.append(h)
                elif h.modalities & {"SR", "CT"} and (needs_ct_dose(session, h.uid)
                                                      or needs_ct_sr(session, h.uid)):
                    ct_dose.append(h)
                else:
                    stats.add("skipped_known")
                continue
            todo.append(h)
    progress(f"{day}: {len(hits)} studies, {len(todo)} to retrieve"
             + (f", {len(ct_dose)} PET/CT for the CT dose report" if ct_dose else ""))
    if dry_run:
        stats.add("would_retrieve", len(todo) + len(ct_dose))
        return
    for h in ct_dose:
        try:
            fetch_sr(node, h.uid, spool, drain, stats)
            stats.add("ct_dose_studies")
        except Exception as e:  # keep going
            stats.add("study_errors")
            log.warning("CT dose report of %s failed: %s", h.uid, e)
        time.sleep(pause)
    lock = threading.Lock()

    def locked_drain() -> int:  # imports are serialised; retrievals run in parallel
        with lock:
            return drain()

    def one(h: StudyHit) -> None:
        try:
            retrieve_study(node, h, spool, locked_drain, session_factory, stats)
            stats.add("studies_done")
        except Exception as e:  # keep going; the day is retried next run
            stats.add("study_errors")
            log.warning("study %s failed: %s", h.uid, e)
        time.sleep(pause)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for i, _ in enumerate(pool.map(one, todo), 1):
            if i % 25 == 0:
                progress(f"{day}: {i}/{len(todo)}")


def staff_backfill(node: Node, day: dt.date, spool: Spool, drain: Callable[[], int],
                   session_factory, stats: QRStats, pause: float = 0.2,
                   progress: Callable[[str], None] = print) -> None:
    """CT studies of the day already in the database without operator or physician: fetch
    one image header each (studies of local devices only)."""
    from ..db.models import Device

    lo = dt.datetime.combine(day, dt.time())
    with session_factory() as session:
        uids = list(session.scalars(
            select(Study.study_uid).join(Device, Device.id == Study.device_id)
            .where(Study.modality == "CT", Device.is_local.is_(True),
                   Study.operator_name.is_(None), Study.performing_physician.is_(None),
                   Study.study_datetime >= lo,
                   Study.study_datetime < lo + dt.timedelta(days=1))))
    progress(f"{day}: {len(uids)} CT studies without staff names")
    for uid in uids:
        try:
            fetch_staff(node, uid, find_series(node, uid), spool, drain, stats)
            stats.add("staff_studies")
        except Exception as e:  # keep going
            stats.add("study_errors")
            log.warning("staff for %s failed: %s", uid, e)
        time.sleep(pause)


def ct_dose_backfill(node: Node, day: dt.date, spool: Spool, drain: Callable[[], int],
                     session_factory, stats: QRStats, pause: float = 0.2,
                     progress: Callable[[str], None] = print) -> None:
    """NM / PET studies of the day already in the database without a CT dose and without
    any SR seen: fetch their SR series (the CT dose report of a PET/CT)."""
    lo = dt.datetime.combine(day, dt.time())
    with session_factory() as session:
        uids = [u for u in session.scalars(
            select(Study.study_uid).where(Study.modality.in_(NM_IMAGES),
                                          Study.total_dlp.is_(None),
                                          Study.study_datetime >= lo,
                                          Study.study_datetime < lo + dt.timedelta(days=1)))
                if needs_ct_dose(session, u)]
    if uids:
        progress(f"{day}: {len(uids)} NM / PET studies without a CT dose")
    for uid in uids:
        try:
            fetch_sr(node, uid, spool, drain, stats)
            stats.add("ct_dose_studies")
        except Exception as e:  # keep going
            stats.add("study_errors")
            log.warning("CT dose report of %s failed: %s", uid, e)
        time.sleep(pause)


def done_days(session: Session) -> set[str]:
    row = session.get(AppSetting, DONE_KEY)
    return set(row.value or []) if row else set()


def mark_day_done(session: Session, day: dt.date) -> None:
    row = session.get(AppSetting, DONE_KEY) or AppSetting(key=DONE_KEY, value=[])
    days = set(row.value or [])
    days.add(day.isoformat())
    row.value = sorted(days)
    session.add(row)
    session.commit()
