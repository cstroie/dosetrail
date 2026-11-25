"""Maintenance jobs that an admin can start from the web (Admin → Maintenance) and that the
command line and cron run the same way.

A job is a ``dozimetrix`` command (``maintain``, ``classify``, ``recompute``). The web server
never runs one itself: it starts the command as a detached process, so a restart of the server
does not kill it and a long reclassify does not hold a web worker. Whoever runs a job, the
command takes one lock file (``<data_dir>/maintain.lock``), so two jobs never overlap (two
reclassifies deleting and inserting the same rows fail with a lock timeout), and writes its
state to ``app_setting`` ``maintenance.job`` (running / done / failed, who, the last lines,
the result): the page and the live stream read that.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session

from .db.models import AppSetting

log = logging.getLogger(__name__)

JOB_KEY = "maintenance.job"
LOCK_NAME = "maintain.lock"
LOG_NAME = "jobs.log"
LOG_LINES = 60
WRITE_EVERY_S = 2.0
START_GRACE_S = 20  # a job started from the web takes a moment to open its lock

JOBS: dict[str, dict] = {
    "maintain": {
        "label": "Periodic maintenance",
        "description": "Reclassify the exams, refill the stored size bands, refresh the clinical "
                       "indications and the device counters, retry the studies whose derived values "
                       "failed. The nightly run does this for the last 90 days; it covers every "
                       "study by itself when the classification rules have changed.",
        "days": {"default": 90, "every": "Every study"},
    },
    "classify": {
        "label": "Classify exams",
        "description": "Assign the standard exam codes and DRL codes again (after editing the rules "
                       "or a protocol mapping). Decisions made by a person are kept.",
        "days": {"default": 90, "every": "Every study"},
    },
    "recompute": {
        "label": "Recompute derived values",
        "description": "Recompute the columns derived from the events (CT counts, device counters, "
                       "SSDE, ESAK, the stored bands, pseudonyms) for every study: the repair after "
                       "a data fix or an import.",
        "days": None,
    },
}


class Busy(RuntimeError):
    """Another maintenance job holds the lock."""


def lock_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / LOCK_NAME


@contextmanager
def locked(path: Path, retries: int = 5):
    """Hold the job lock (``Busy`` when someone else has it). A few short retries ride out the
    instant another process holds it only to see whether it is free (``is_busy``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")  # noqa: SIM115 – closed in the finally, which releases the lock
    try:
        for attempt in range(retries + 1):
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if attempt == retries:
                    raise Busy("another maintenance job is running") from e
                time.sleep(0.1)
        yield
    finally:
        f.close()


def is_busy(path: Path) -> bool:
    try:
        with locked(path, retries=0):
            return False
    except Busy:
        return True


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _store(maker, state: dict) -> None:
    try:
        with maker() as session:
            row = session.get(AppSetting, JOB_KEY) or AppSetting(key=JOB_KEY)
            row.value = dict(state)
            session.add(row)
            session.commit()
    except Exception:  # noqa: BLE001 – the state is for display; never stop the job for it
        log.exception("could not store the job state")


def execute(maker, name: str, run, options: dict, by: str, path: Path, say=print) -> object:
    """Run ``run(progress)`` as job ``name`` under the lock and record its state. ``Busy`` when
    another job is running. Returns what ``run`` returns; an exception is recorded and raised."""
    with locked(path):
        state = {"job": name, "options": options, "by": by, "state": "running", "pid": os.getpid(),
                 "started": _now(), "lines": []}
        _store(maker, state)
        last = [time.monotonic()]

        def progress(msg: str) -> None:
            say(msg)
            state["lines"] = (state["lines"] + [str(msg)[:300]])[-LOG_LINES:]
            if time.monotonic() - last[0] >= WRITE_EVERY_S:
                last[0] = time.monotonic()
                _store(maker, state)

        try:
            result = run(progress)
        except BaseException as e:
            state.update(state="failed", finished=_now(), error=f"{type(e).__name__}: {e}")
            _store(maker, state)
            raise
        ok = result.get("ok", True) if isinstance(result, dict) else True
        state.update(state="done" if ok else "failed", finished=_now(),
                     result=result if isinstance(result, dict) else None)
        _store(maker, state)
        return result


def current(session: Session, path: Path) -> dict | None:
    """The last job's state; a job that says it runs but holds no lock (the process died, the
    machine restarted) shows as interrupted."""
    row = session.get(AppSetting, JOB_KEY)
    st = dict(row.value) if row and row.value else None
    if st and st.get("state") == "running" and not is_busy(path):
        started = dt.datetime.fromisoformat(st["started"]) if st.get("started") else None
        if started is None or (dt.datetime.now() - started).total_seconds() > START_GRACE_S:
            st["state"] = "interrupted"
    return st


def overview(session: Session, path: Path) -> dict:
    from . import maintenance

    last = session.get(AppSetting, maintenance.STATUS_KEY)
    stored = session.get(AppSetting, maintenance.RULES_KEY)
    return {"jobs": [{"name": n, "label": j["label"], "description": j["description"],
                      "days": j["days"]} for n, j in JOBS.items()],
            "current": current(session, path),
            "last_run": dict(last.value) if last and last.value else None,
            "rules_changed": (stored.value or {}).get("hash") != maintenance.rules_fingerprint()
            if stored else True}


def argv(name: str, days: int | None, by: str) -> list[str]:
    """The command line of a job: ``dozimetrix <this>``."""
    if name == "maintain":
        out = ["maintain", "--days", str(days)] if days else ["maintain", "--full"]
    elif name == "classify":
        out = ["classify", "--days", str(days)] if days else ["classify"]
    else:
        out = ["recompute"]
    return [*out, "--by", by]


def start(settings, session: Session, name: str, days: int | None, by: str) -> dict:
    """Start job ``name`` as a detached process (``Busy`` when one runs). The state is written
    at once, so the page shows it before the process has opened its lock."""
    if name not in JOBS:
        raise KeyError(name)
    path = lock_path(settings.data_dir)
    if is_busy(path):
        raise Busy("another maintenance job is running")
    cmd = [sys.executable, "-m", "dozimetrix.cli"]
    if settings.source:
        cmd += ["--config", str(settings.source)]
    cmd += argv(name, days, by)
    if not settings.source:
        cmd += ["--url", settings.database_url]
    logfile = path.parent / LOG_NAME
    logfile.parent.mkdir(parents=True, exist_ok=True)
    state = {"job": name, "options": {"days": days}, "by": by, "state": "running", "pid": None,
             "started": _now(), "lines": []}
    row = session.get(AppSetting, JOB_KEY) or AppSetting(key=JOB_KEY)
    row.value = state
    session.add(row)
    session.commit()
    with open(logfile, "ab") as out:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,  # noqa: S603
                         start_new_session=True, cwd=Path.cwd())
    return state
