"""Configuration: TOML file plus environment overrides (stdlib only).

Lookup order for the config file: $DOSETRAIL_CONFIG, ./dosetrail.toml,
~/.config/dosetrail/dosetrail.toml. Environment variables override file values:
DOSETRAIL_DATABASE_URL.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DB_URL = "sqlite:///dosetrail.sqlite3"


@dataclass
class Settings:
    database_url: str = DEFAULT_DB_URL
    echo_sql: bool = False
    data_dir: Path = field(default_factory=lambda: Path("var"))
    secret_file: Path = field(
        default_factory=lambda: Path.home() / ".config" / "dosetrail" / "secret")
    session_hours: float = 12.0
    secure_cookies: bool = False  # set true behind HTTPS
    # DICOM networking
    dicom_aet: str = "DOSETRAIL"
    dicom_port: int = 11112
    dicom_bind: str = "0.0.0.0"
    dicom_allowed_aets: list[str] = field(default_factory=list)  # empty = accept any caller
    spool_dir: Path = field(default_factory=lambda: Path("var/spool"))
    keep_received: bool = False  # keep imported objects under spool/archive/YYYYMMDD
    source: Path | None = None


def _candidate_files() -> list[Path]:
    env = os.environ.get("DOSETRAIL_CONFIG")
    if env:
        return [Path(env)]
    return [Path("dosetrail.toml"), Path.home() / ".config" / "dosetrail" / "dosetrail.toml"]


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
            dcm = raw.get("dicom", {})
            s.dicom_aet = dcm.get("aet", s.dicom_aet)
            s.dicom_port = int(dcm.get("port", s.dicom_port))
            s.dicom_bind = dcm.get("bind", s.dicom_bind)
            s.dicom_allowed_aets = list(dcm.get("allowed_aets", s.dicom_allowed_aets))
            if "spool_dir" in dcm:
                s.spool_dir = Path(dcm["spool_dir"]).expanduser()
            s.keep_received = bool(dcm.get("keep_received", s.keep_received))
            sec = raw.get("security", {})
            if "secret_file" in sec:
                s.secret_file = Path(sec["secret_file"]).expanduser()
            s.session_hours = float(sec.get("session_hours", s.session_hours))
            s.secure_cookies = bool(sec.get("secure_cookies", s.secure_cookies))
            s.source = path
            break
    s.database_url = os.environ.get("DOSETRAIL_DATABASE_URL", s.database_url)
    if os.environ.get("DOSETRAIL_SECRET_FILE"):
        s.secret_file = Path(os.environ["DOSETRAIL_SECRET_FILE"])
    return s
