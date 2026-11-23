"""Configuration: TOML file plus environment overrides (stdlib only).

Lookup order for the config file: $DOSIMETRIX_CONFIG, ./dosimetrix.toml,
~/.config/dosimetrix/dosimetrix.toml. Environment variables override file values:
DOSIMETRIX_DATABASE_URL, DOSIMETRIX_SECRET_FILE.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_URL = "sqlite:///dosimetrix.sqlite3"


@dataclass
class Settings:
    database_url: str = DEFAULT_DB_URL
    echo_sql: bool = False
    data_dir: Path = field(default_factory=lambda: Path("var"))
    secret_file: Path = field(default_factory=lambda: _config_dir() / "secret")
    session_hours: float = 12.0
    secure_cookies: bool = False  # set true behind HTTPS
    # web UI / API
    web_host: str = "127.0.0.1"
    web_port: int = 8104
    web_trusted_proxies: list[str] = field(default_factory=lambda: ["127.0.0.1"])
    # DICOM networking
    dicom_aet: str = "DOSIMETRIX"
    dicom_port: int = 11112
    dicom_bind: str = "0.0.0.0"
    dicom_allowed_aets: list[str] = field(default_factory=list)  # empty = accept any caller
    spool_dir: Path = field(default_factory=lambda: Path("var/spool"))
    keep_received: bool = False  # keep imported objects under spool/archive/YYYYMMDD
    # PACS for query/retrieve (C-FIND + C-GET)
    pacs_host: str | None = None
    pacs_port: int = 104
    pacs_aet: str | None = None
    pacs_retrieve: str = "get"  # get | move
    pacs_move_destination: str | None = None  # AE the PACS moves to (default: dicom_aet)
    pacs_modalities: list[str] = field(
        default_factory=lambda: ["CT", "DX", "CR", "RF", "XA", "MG", "NM", "PT", "SR"])
    pacs_max_results: int | None = None  # the PACS C-FIND cap, if any
    source: Path | None = None
    notify: dict = field(default_factory=dict)  # [notify] table, see dosimetrix/notify.py


def _env(name: str) -> str | None:
    """DOSIMETRIX_<name>."""
    return os.environ.get(f"DOSIMETRIX_{name}")


def _config_dir() -> Path:
    return Path.home() / ".config" / "dosimetrix"


def _candidate_files() -> list[Path]:
    env = _env("CONFIG")
    if env:
        return [Path(env)]
    d = _config_dir()
    return [Path("dosimetrix.toml"), d / "dosimetrix.toml"]


def load_settings() -> Settings:
    s = Settings()
    for path in _candidate_files():
        if path.is_file():
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
            db = raw.get("database", {})
            s.database_url = db.get("url", s.database_url)
            s.echo_sql = bool(db.get("echo", s.echo_sql))
            if "data_dir" in raw.get("paths", {}):
                s.data_dir = Path(raw["paths"]["data_dir"])
            web = raw.get("web", {})
            s.web_host = web.get("host", s.web_host)
            s.web_port = int(web.get("port", s.web_port))
            s.web_trusted_proxies = list(web.get("trusted_proxies", s.web_trusted_proxies))
            dcm = raw.get("dicom", {})
            s.dicom_aet = dcm.get("aet", s.dicom_aet)
            s.dicom_port = int(dcm.get("port", s.dicom_port))
            s.dicom_bind = dcm.get("bind", s.dicom_bind)
            s.dicom_allowed_aets = list(dcm.get("allowed_aets", s.dicom_allowed_aets))
            if "spool_dir" in dcm:
                s.spool_dir = Path(dcm["spool_dir"]).expanduser()
            s.keep_received = bool(dcm.get("keep_received", s.keep_received))
            pacs = raw.get("pacs", {})
            s.pacs_host = pacs.get("host", s.pacs_host)
            s.pacs_port = int(pacs.get("port", s.pacs_port))
            s.pacs_aet = pacs.get("aet", s.pacs_aet)
            s.pacs_retrieve = pacs.get("retrieve", s.pacs_retrieve)
            s.pacs_move_destination = pacs.get("move_destination", s.pacs_move_destination)
            s.pacs_modalities = list(pacs.get("modalities", s.pacs_modalities))
            s.pacs_max_results = pacs.get("max_results", s.pacs_max_results)
            sec = raw.get("security", {})
            if "secret_file" in sec:
                s.secret_file = Path(sec["secret_file"]).expanduser()
            s.session_hours = float(sec.get("session_hours", s.session_hours))
            s.secure_cookies = bool(sec.get("secure_cookies", s.secure_cookies))
            s.notify = dict(raw.get("notify", {}))
            s.source = path
            break
    s.database_url = _env("DATABASE_URL") or s.database_url
    if _env("SECRET_FILE"):
        s.secret_file = Path(_env("SECRET_FILE"))
    return s
