"""Query/retrieve from a PACS: C-FIND (study, series) and C-GET into the spool.

Selection per study (OpenREM's proven order, adapted):
1. skip studies without X-ray modalities (US, MR…) and studies already in the database;
2. retrieve the SR series first and import them;
3. for projection radiography (DX/CR/MG/RF/XA) retrieve the image series only if no dose
   report covered the study – their headers carry the dose; CT images are never fetched.

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
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AppSetting, Study
from .spool import Spool

log = logging.getLogger("dosetrail.qr")

XRAY = {"CT", "DX", "CR", "MG", "RF", "XA", "SR", "NM", "PT", "IO", "PX"}
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
]
DONE_KEY = "qr.backfill.done_days"


@dataclass
class Node:
    host: str
    port: int
    aet: str  # the PACS
    calling_aet: str = "DOSETRAIL"
    timeout: float = 60


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


def find_studies(node: Node, day: dt.date) -> list[StudyHit]:
    q = Dataset()
    q.QueryRetrieveLevel = "STUDY"
    q.StudyDate = day.strftime("%Y%m%d")
    q.StudyInstanceUID = ""
    q.ModalitiesInStudy = ""
    q.StudyDescription = ""
    q.NumberOfStudyRelatedInstances = ""
    assoc = _assoc_find(node)
    try:
        rows = _find(assoc, q)
    finally:
        assoc.release()
    hits = {}
    for r in rows:
        uid = str(r.get("StudyInstanceUID", "")).strip()
        if uid:
            n = r.get("NumberOfStudyRelatedInstances")
            hits[uid] = StudyHit(uid, q.StudyDate, _modalities(r.get("ModalitiesInStudy")),
                                 str(r.get("StudyDescription", "") or ""),
                                 int(n) if n not in (None, "") else None)
    return list(hits.values())


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


def get_series(node: Node, study_uid: str, series_uids: list[str], spool: Spool) -> dict:
    """C-GET the series into the spool; returns delivered/claimed counts."""
    delivered = 0

    def on_store(event) -> int:
        nonlocal delivered
        data = event.encoded_dataset()
        spool.write(str(event.request.AffectedSOPInstanceUID), lambda p: p.write_bytes(data))
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
            q.QueryRetrieveLevel = "SERIES"
            q.StudyInstanceUID = study_uid
            q.SeriesInstanceUID = uid
            last = None
            for status, _ident in assoc.send_c_get(q, GET):
                if status:
                    last = status
            if last is not None:
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


def retrieve_study(node: Node, hit: StudyHit, spool: Spool, drain: Callable[[], int],
                   session_factory, stats: QRStats, max_images: int = 50) -> None:
    series = find_series(node, hit.uid)
    sr = [s["uid"] for s in series if s["modality"] == "SR"]
    if sr:
        r = get_series(node, hit.uid, sr, spool)
        stats.add("sr_objects", r["delivered"])
        if r["claimed"] > r["delivered"]:
            stats.add("not_delivered", r["claimed"] - r["delivered"])
        drain()
    with session_factory() as session:
        covered = has_dose_report(session, hit.uid)
    if covered:
        stats.add("studies_by_dose_report")
        return
    images = [s for s in series if s["modality"] in PROJECTION_IMAGES
              and (s["instances"] is None or s["instances"] <= max_images)]
    if images:
        r = get_series(node, hit.uid, [s["uid"] for s in images], spool)
        stats.add("image_objects", r["delivered"])
        if r["claimed"] > r["delivered"]:
            stats.add("not_delivered", r["claimed"] - r["delivered"])
        drain()
        stats.add("studies_by_headers")
    elif not sr:
        stats.add("studies_without_dose_objects")


def run_day(node: Node, day: dt.date, spool: Spool, drain: Callable[[], int],
            session_factory, stats: QRStats, modalities: set[str] | None = None,
            dry_run: bool = False, pause: float = 0.2,
            progress: Callable[[str], None] = print, workers: int = 1) -> None:
    hits = find_studies(node, day)
    wanted = modalities or (XRAY - {"SR"})
    todo = []
    with session_factory() as session:
        for h in hits:
            mods = h.modalities - {"SR", "PR", "KO", "OT", "DOC"}
            if not (mods & wanted) and not (h.modalities == {"SR"} and "SR" in XRAY):
                stats.add("skipped_not_xray")
                continue
            if in_database(session, h.uid):
                stats.add("skipped_known")
                continue
            todo.append(h)
    progress(f"{day}: {len(hits)} studies, {len(todo)} to retrieve")
    if dry_run:
        stats.add("would_retrieve", len(todo))
        return
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
