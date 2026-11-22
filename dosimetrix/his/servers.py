"""HIS servers (stored in the database, edited in Admin → Integration).

The password is write-only: it is sealed with the site secret (see secretbox.py), never
returned by the API and never written to the audit log; `as_dict` only says whether it is set.
"""

from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import secretbox
from ..db.models import AppSetting, HisServer

STATUS_KEY = "his.status"
KINDS = ("hippobridge",)
FIELDS = ("name", "kind", "base_url", "username", "enabled", "timeout", "modalities",
          "window_before_h", "window_after_h", "min_interval_s", "backfill_hours", "notes")


class ServerError(ValueError):
    pass


def as_dict(s: HisServer) -> dict:
    d = {f: getattr(s, f) for f in ("id", *FIELDS)}
    d["password_set"] = bool(s.password_enc)
    d["last_check"] = None if s.last_check_at is None else {
        "at": s.last_check_at.isoformat(timespec="seconds"), "ok": s.last_check_ok,
        "ms": s.last_check_ms, "error": s.last_check_error}
    return d


def _clean(patch: dict) -> dict:
    out = {k: v for k, v in patch.items() if k in FIELDS}
    for k in ("name", "base_url", "username", "modalities", "backfill_hours", "notes"):
        if k in out and isinstance(out[k], str):
            out[k] = out[k].strip() or None
    if out.get("kind") is not None and out["kind"] not in KINDS:
        raise ServerError(f"kind is one of: {', '.join(KINDS)}")
    if "base_url" in out:
        if not re.fullmatch(r"https?://[^\s/]+(/\S*)?", out["base_url"] or ""):
            raise ServerError("base URL: http(s)://host[:port][/path]")
        out["base_url"] = out["base_url"].rstrip("/")
    if out.get("modalities"):
        out["modalities"] = ",".join(m.strip().upper() for m in out["modalities"].split(",")
                                     if m.strip())
    if out.get("backfill_hours") and not re.fullmatch(r"([01]?\d|2[0-3])-([01]?\d|2[0-3])",
                                                      out["backfill_hours"]):
        raise ServerError("backfill hours: start-end, e.g. 22-06")
    for k, lo, hi in (("timeout", 5, 600), ("window_before_h", 0, 24 * 30),
                      ("window_after_h", 0, 48), ("min_interval_s", 0, 600)):
        if out.get(k) is not None and not lo <= float(out[k]) <= hi:
            raise ServerError(f"{k} must be {lo}–{hi}")
    return out


def _password(s: HisServer, secret: bytes, data: dict) -> bool:
    """Apply a new password from `data` ('' clears it); True when it changed."""
    if "password" not in data or data["password"] is None:
        return False
    s.password_enc = secretbox.seal(secret, data["password"]) if data["password"] else None
    return True


def create(session: Session, secret: bytes, data: dict) -> HisServer:
    d = _clean(data)
    if not d.get("name") or not d.get("base_url"):
        raise ServerError("name and base URL are required")
    if session.scalar(select(HisServer).filter_by(name=d["name"])) is not None:
        raise ServerError(f"a server named {d['name']!r} exists")
    s = HisServer(**d)
    _password(s, secret, data)
    session.add(s)
    session.flush()
    return s


def update(session: Session, secret: bytes, server_id: int,
           patch: dict) -> tuple[HisServer, dict]:
    s = session.get(HisServer, server_id)
    if s is None:
        raise ServerError("server not found")
    d = _clean(patch)
    changes = {k: {"from": getattr(s, k), "to": v} for k, v in d.items() if getattr(s, k) != v}
    for k, v in d.items():
        setattr(s, k, v)
    if _password(s, secret, patch):
        changes["password"] = "changed"  # never the value
    session.flush()
    return s, changes


def delete(session: Session, server_id: int) -> dict:
    s = session.get(HisServer, server_id)
    if s is None:
        raise ServerError("server not found")
    d = as_dict(s)
    session.delete(s)
    session.flush()
    return d


def client(s: HisServer, secret: bytes):
    """A client for the server (only HippoBridge so far)."""
    from .hippobridge import Client

    password = secretbox.open_(secret, s.password_enc) if s.password_enc else None
    return Client(s.base_url, s.username, password, timeout=s.timeout)


def check(session: Session, secret: bytes, server_id: int) -> dict:
    """Reach the server and verify the login; the outcome is stored on the server."""
    s = session.get(HisServer, server_id)
    if s is None:
        raise ServerError("server not found")
    try:
        res = client(s, secret).check()
    except secretbox.SecretError as e:
        res = {"ok": False, "ms": 0, "error": str(e)}
    s.last_check_at = dt.datetime.now()
    s.last_check_ok, s.last_check_ms = res["ok"], res["ms"]
    s.last_check_error = (res["error"] or None) and res["error"][:255]
    session.flush()
    return as_dict(s)["last_check"]


def active(session: Session) -> list[HisServer]:
    return list(session.scalars(select(HisServer).where(HisServer.enabled.is_(True))
                                .order_by(HisServer.id)))


def covers(s: HisServer, modality: str | None) -> bool:
    mods = [m for m in (s.modalities or "").split(",") if m]
    return not mods or (modality or "") in mods


def write_status(session: Session, value: dict) -> None:
    row = session.get(AppSetting, STATUS_KEY) or AppSetting(key=STATUS_KEY)
    row.value = {**value, "at": dt.datetime.now().isoformat(timespec="seconds")}
    session.add(row)
    session.flush()


def reseal_secrets(session: Session, secret: bytes) -> int:
    """Passwords sealed under the former name's key, sealed again with the current one."""
    n = 0
    for srv in session.scalars(select(HisServer).where(HisServer.password_enc.is_not(None))):
        if not secretbox.is_current(secret, srv.password_enc):
            srv.password_enc = secretbox.reseal(secret, srv.password_enc)
            n += 1
    session.flush()
    return n

