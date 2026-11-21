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
            s.source = path
            break
    s.database_url = os.environ.get("DOSETRAIL_DATABASE_URL", s.database_url)
    return s
