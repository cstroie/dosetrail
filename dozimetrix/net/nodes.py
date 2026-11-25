"""DICOM nodes (stored in the database, edited in the UI), C-ECHO, and service status.

* A node is a remote AE. Its two roles can be combined:
  * ``query_retrieve``: a PACS we query and retrieve from;
  * ``sends``: allowed to send to our receiver.
* The receiver accepts the ``sends`` AE titles plus ``[dicom] allowed_aets`` from the
  config. If both are empty, it accepts any caller. It re-reads the list every minute.
* ``[pacs]`` from the config is imported once as a node, so existing setups keep working.
  Deleting that node is respected: it is not re-imported.
* Status heartbeats (``app_setting``): the receiver writes ``receiver.status`` every 30 s,
  and a running ``qr`` writes ``qr.status``.
"""

from __future__ import annotations

import datetime as dt
import os
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AppSetting, DicomNode

IMPORTED_KEY = "dicom.config_imported"
RECEIVER_KEY, QR_KEY = "receiver.status", "qr.status"
# query/retrieve is a scheduled run (hourly timer, or every 15 min), not a daemon: it is only
# late when a whole period and more has passed without one
QR_STALE_S = 5400
FIELDS = ("name", "aet", "host", "port", "enabled", "query_retrieve", "sends", "retrieve",
          "move_destination", "modalities", "max_results", "timeout", "sync_days", "notes")


class NodeError(ValueError):
    pass


def as_dict(n: DicomNode) -> dict:
    d = {f: getattr(n, f) for f in ("id", *FIELDS)}
    d["last_echo"] = None if n.last_echo_at is None else {
        "at": n.last_echo_at.isoformat(timespec="seconds"), "ok": n.last_echo_ok,
        "ms": n.last_echo_ms, "error": n.last_echo_error}
    return d


def import_config(session: Session, settings) -> DicomNode | None:
    """Create a node from ``[pacs]`` once (when configured and never imported before)."""
    if session.get(AppSetting, IMPORTED_KEY) is not None or not settings.pacs_host:
        return None
    node = None
    if session.scalar(select(DicomNode).filter_by(aet=settings.pacs_aet)) is None:
        node = DicomNode(name="PACS", aet=settings.pacs_aet, host=settings.pacs_host,
                         port=settings.pacs_port, query_retrieve=True,
                         retrieve=settings.pacs_retrieve,
                         move_destination=settings.pacs_move_destination,
                         modalities=",".join(settings.pacs_modalities),
                         max_results=settings.pacs_max_results, sync_days=2,
                         notes="Imported from [pacs] in dozimetrix.toml")
        session.add(node)
    session.add(AppSetting(key=IMPORTED_KEY, value={"at": dt.datetime.now().isoformat()}))
    session.flush()
    return node


def _clean(patch: dict) -> dict:
    out = {k: v for k, v in patch.items() if k in FIELDS}
    for k in ("name", "aet", "host", "move_destination", "modalities", "notes"):
        if k in out and isinstance(out[k], str):
            out[k] = out[k].strip() or None
    if "aet" in out:
        aet = out["aet"] or ""
        if not aet or len(aet) > 16 or not aet.isascii() or "\\" in aet:
            raise NodeError("AE title: 1–16 ASCII characters, no backslash")
        out["aet"] = aet
    if out.get("port") is not None and not 1 <= int(out["port"]) <= 65535:
        raise NodeError("port must be 1–65535")
    if out.get("retrieve") not in (None, "get", "move"):
        raise NodeError("retrieve is get or move")
    if out.get("modalities"):
        out["modalities"] = ",".join(m.strip().upper() for m in out["modalities"].split(",")
                                     if m.strip())
    return out


def create(session: Session, data: dict) -> DicomNode:
    d = _clean(data)
    if not d.get("name") or not d.get("aet"):
        raise NodeError("name and AE title are required")
    if session.scalar(select(DicomNode).filter_by(name=d["name"])) is not None:
        raise NodeError(f"a node named {d['name']!r} exists")
    n = DicomNode(**d)
    _check(n)
    session.add(n)
    session.flush()
    return n


def update(session: Session, node_id: int, patch: dict) -> tuple[DicomNode, dict]:
    n = session.get(DicomNode, node_id)
    if n is None:
        raise NodeError("node not found")
    d = _clean(patch)
    changes = {k: {"from": getattr(n, k), "to": v} for k, v in d.items() if getattr(n, k) != v}
    for k, v in d.items():
        setattr(n, k, v)
    _check(n)
    session.flush()
    return n, changes


def _check(n: DicomNode) -> None:
    if n.query_retrieve and (not n.host or not n.port):
        raise NodeError("a query/retrieve node needs host and port")


def delete(session: Session, node_id: int) -> dict:
    n = session.get(DicomNode, node_id)
    if n is None:
        raise NodeError("node not found")
    d = as_dict(n)
    session.delete(n)
    session.flush()
    return d


def echo(session: Session, node_id: int, calling_aet: str, timeout: float = 10) -> dict:
    """C-ECHO the node and record the outcome."""
    from pynetdicom import AE
    from pynetdicom.sop_class import Verification

    n = session.get(DicomNode, node_id)
    if n is None:
        raise NodeError("node not found")
    if not n.host or not n.port:
        raise NodeError("the node has no host/port to echo")
    ae = AE(ae_title=calling_aet)
    ae.add_requested_context(Verification)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = timeout
    started = time.monotonic()
    ok, error = False, None
    try:
        assoc = ae.associate(n.host, n.port, ae_title=n.aet)
        if assoc.is_established:
            status = assoc.send_c_echo()
            ok = bool(status) and status.Status == 0x0000
            error = None if ok else f"status 0x{status.Status:04X}" if status else "no response"
            assoc.release()
        else:
            error = "association rejected or aborted" if assoc.is_rejected or assoc.is_aborted \
                else "no association (host unreachable or wrong port)"
    except Exception as exc:  # noqa: BLE001 – report any network error
        error = str(exc)[:255]
    n.last_echo_at = dt.datetime.now()
    n.last_echo_ok, n.last_echo_ms = ok, int((time.monotonic() - started) * 1000)
    n.last_echo_error = error
    session.flush()
    return as_dict(n)["last_echo"]


def allowed_senders(session: Session, config_aets: list[str]) -> list[str]:
    """AE titles the receiver accepts; empty = any caller."""
    db = session.scalars(select(DicomNode.aet).where(DicomNode.enabled.is_(True),
                                                     DicomNode.sends.is_(True)))
    return sorted({*config_aets, *db})


def qr_node(session: Session, settings, name: str | None = None):
    """The PACS to query: a named node, else the first enabled query/retrieve node, else
    ``[pacs]`` from the config. Returns (qr.Node, modalities) or (None, None)."""
    from . import qr

    q = select(DicomNode).where(DicomNode.enabled.is_(True), DicomNode.query_retrieve.is_(True))
    n = session.scalar(q.filter_by(name=name) if name else q.order_by(DicomNode.id))
    if n is not None:
        mods = [m for m in (n.modalities or "").split(",") if m] or list(settings.pacs_modalities)
        return qr.Node(n.host, n.port, n.aet, settings.dicom_aet, timeout=n.timeout or 60,
                       retrieve=n.retrieve or "get", move_destination=n.move_destination,
                       max_results=n.max_results), mods
    if name or not settings.pacs_host:
        return None, None
    return qr.Node(settings.pacs_host, settings.pacs_port, settings.pacs_aet,
                   settings.dicom_aet, retrieve=settings.pacs_retrieve,
                   move_destination=settings.pacs_move_destination,
                   max_results=settings.pacs_max_results), list(settings.pacs_modalities)


def write_status(session: Session, key: str, value: dict) -> None:
    row = session.get(AppSetting, key) or AppSetting(key=key)
    row.value = {**value, "at": dt.datetime.now().isoformat(timespec="seconds"),
                 "pid": os.getpid()}
    session.add(row)
    session.flush()


def read_status(session: Session, key: str, stale_after: float) -> dict | None:
    row = session.get(AppSetting, key)
    if row is None or not row.value:
        return None
    v = dict(row.value)
    at = dt.datetime.fromisoformat(v["at"])
    v["age_s"] = int((dt.datetime.now() - at).total_seconds())
    v["running"] = v["age_s"] <= stale_after
    return v
