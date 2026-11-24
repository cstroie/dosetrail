"""Dump an OpenREM PostgreSQL database to flat, gzipped CSV files (read-only).

Runs `psql` either locally or on a remote host through ssh. Every statement executes in a
READ ONLY transaction; nothing is written on the source server. Output files contain
patient identifiers – the dump directory is created with mode 0700 and files 0600.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .queries import DUMPS


@dataclass
class Source:
    db: str
    ssh: str | None = None  # e.g. "root@host"; None = local psql
    sudo_postgres: bool = True  # run psql as the postgres OS user (peer auth)

    def command(self) -> list[str]:
        psql = f"psql -X -q -v ON_ERROR_STOP=1 -d {shlex.quote(self.db)}"
        if self.sudo_postgres:
            psql = f"cd /tmp && sudo -u postgres {psql}"
        remote = f"{psql} | gzip -c"
        if self.ssh:
            return ["ssh", "-o", "BatchMode=yes", self.ssh, remote]
        return ["sh", "-c", remote]


def _one_line(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def dump_query(source: Source, sql: str, out: Path) -> int:
    """Stream `\\copy (sql) TO STDOUT CSV HEADER` into a gzip file; return data row count."""
    script = (
        "SET default_transaction_read_only = on;\n"
        "SET TIME ZONE LOCAL;\n"
        f"\\copy ({_one_line(sql)}) TO STDOUT WITH (FORMAT csv, HEADER true)\n"
    )
    tmp = out.with_suffix(out.suffix + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        proc = subprocess.run(source.command(), input=script.encode(), stdout=fh,
                              stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"dump of {out.name} failed: {proc.stderr.decode(errors='replace')}")
    tmp.replace(out)
    with gzip.open(out, "rt", encoding="utf-8", newline="") as fh:
        return max(sum(1 for _ in fh) - 1, 0)  # approximate (multi-line fields are rare)


def dump_all(source: Source, out_dir: Path, only: list[str] | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    manifest = {
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "source": {"db": source.db, "via_ssh": bool(source.ssh)},
        "files": {},
    }
    for name, sql in DUMPS.items():
        if only and name not in only:
            continue
        path = out_dir / f"{name}.csv.gz"
        started = dt.datetime.now()
        rows = dump_query(source, sql, path)
        manifest["files"][name] = {
            "rows": rows, "bytes": path.stat().st_size,
            "seconds": round((dt.datetime.now() - started).total_seconds(), 1),
        }
        print(f"{name}: {rows} rows, {path.stat().st_size / 1e6:.1f} MB "
              f"in {manifest['files'][name]['seconds']} s")
    with gzip.open(out_dir / "schema_version.csv.gz", "rt") as fh:
        lines = fh.read().splitlines()
        manifest["schema_version"] = lines[1] if len(lines) > 1 else None
    fd = os.open(out_dir / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest
