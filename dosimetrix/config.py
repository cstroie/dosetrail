"""Configuration: one TOML file, nothing else (stdlib only).

Lookup order: the path given with `dosimetrix --config PATH`, ./dosimetrix.toml,
~/.config/dosimetrix/dosimetrix.toml, /etc/dosimetrix/dosimetrix.toml. The first that exists
is the only one read. It holds the database URL ([database] url, and test_url for the test
suite), the site secret's location, the web, DICOM and PACS settings: no environment
variable changes anything.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_URL = "sqlite:///dosimetrix.sqlite3"


@dataclass
class Settings:
    database_url: str = DEFAULT_DB_URL
    test_database_url: str | None = None  # [database] test_url: a scratch DB for the test suite
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


_explicit: Path | None = None  # set by --config


def set_config_path(path: str | Path | None) -> None:
    """Use this file (it must exist) instead of searching the usual places."""
    global _explicit
    if path and not Path(path).expanduser().is_file():
        raise FileNotFoundError(f"config file not found: {path}")
    _explicit = Path(path).expanduser() if path else None


def _config_dir() -> Path:
    return Path.home() / ".config" / "dosimetrix"


def _candidate_files() -> list[Path]:
    if _explicit is not None:
        return [_explicit]
    return [Path("dosimetrix.toml"), _config_dir() / "dosimetrix.toml",
            Path("/etc/dosimetrix/dosimetrix.toml")]


def load_settings() -> Settings:
    s = Settings()
    for path in _candidate_files():
        if path.is_file():
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
            db = raw.get("database", {})
            s.database_url = db.get("url", s.database_url)
            s.test_database_url = db.get("test_url", s.test_database_url)
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
    return s
