"""Site-wide settings an admin edits in the web UI (Admin → Settings), kept in the database
(``app_setting`` key ``site.settings``) over the value in ``dozimetrix.toml``, so a change applies
without a restart. The site name and code are the first-run wizard's (``setup.SITE_KEY``).

* ``base_url``: the address people open Dozimetrix at; links in notifications use it;
* ``spool_retention_days``: failed and archived DICOM objects older than this are removed
  (0 keeps everything).
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session, sessionmaker

from .db.models import AppSetting

KEY = "site.settings"
MAX_RETENTION_DAYS = 3650


class SiteConfError(ValueError):
    pass


def stored(session: Session) -> dict:
    row = session.get(AppSetting, KEY)
    return dict(row.value or {}) if row else {}


def clean(patch: dict) -> dict:
    """Validate a partial update; an empty value means "back to the toml / default"."""
    out = {}
    if "base_url" in patch:
        v = (patch["base_url"] or "").strip().rstrip("/")
        if v and not re.match(r"^https?://[^\s/]+(/\S*)?$", v):
            raise SiteConfError("the web address must be an http:// or https:// address")
        out["base_url"] = v or None
    if "spool_retention_days" in patch:
        v = patch["spool_retention_days"]
        if v is not None and not 0 <= v <= MAX_RETENTION_DAYS:
            raise SiteConfError(f"retention is 0 (keep everything) to {MAX_RETENTION_DAYS} days")
        out["spool_retention_days"] = v
    return out


def save(session: Session, patch: dict) -> dict:
    """Apply a cleaned update; returns {field: {from, to}} for what changed."""
    before = stored(session)
    after = dict(before)
    for k, v in patch.items():
        if v is None:
            after.pop(k, None)
        else:
            after[k] = v
    row = session.get(AppSetting, KEY) or AppSetting(key=KEY)
    row.value = after
    session.merge(row)
    return {k: {"from": before.get(k), "to": after.get(k)}
            for k in patch if before.get(k) != after.get(k)}


def spool_retention(maker: sessionmaker | None, default: int) -> int:
    """The retention the receiver prunes with: the UI value, else the toml's (read each hour)."""
    if maker is None:
        return default
    try:
        with maker() as session:
            v = stored(session).get("spool_retention_days")
    except Exception:  # noqa: BLE001 – the prune must not depend on the database being up
        return default
    return default if v is None else int(v)
