"""Spool: received objects wait on disk until the worker imports them.

    spool/incoming/   written by the receiver (atomic: *.part then rename); a worker claims a
                      file by renaming it to *.work, so several processes (the receiver
                      service and a query/retrieve run) can share one spool safely
    spool/archive/    imported objects, kept only when keep_received is on (by date)
    spool/failed/     objects that could not be imported, with a .reason file
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import threading
import time
from pathlib import Path

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from ..ingest.files import read
from ..ingest.parse import parse
from ..ingest.store import Store

log = logging.getLogger("dosimetrix.spool")


class Spool:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.incoming = self.root / "incoming"
        self.archive = self.root / "archive"
        self.failed = self.root / "failed"
        for d in (self.incoming, self.archive, self.failed):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o700)  # received objects carry patient identifiers

    def write(self, sop_instance_uid: str, write_fn) -> Path:
        """write_fn(path) writes the object; the rename makes it visible atomically."""
        safe = "".join(c for c in sop_instance_uid if c.isdigit() or c == ".")[:64] or "obj"
        final = self.incoming / f"{safe}.dcm"
        part = final.with_suffix(".part")
        write_fn(part)
        os.chmod(part, 0o600)
        part.replace(final)
        return final

    def pending(self) -> list[Path]:
        files = []
        for p in self.incoming.glob("*.dcm"):
            try:
                files.append((p.stat().st_mtime, p))
            except FileNotFoundError:  # claimed by another worker meanwhile
                continue
        return [p for _, p in sorted(files)]

    def claim(self, path: Path) -> Path | None:
        """Atomically take a pending file; None if another worker was faster."""
        work = path.with_suffix(".work")
        try:
            path.rename(work)
        except FileNotFoundError:
            return None
        return work

    def recover(self, older_than: float = 3600) -> int:
        """Return files left claimed by a crashed worker to the queue."""
        n = 0
        now = time.time()
        for w in self.incoming.glob("*.work"):
            try:
                if now - w.stat().st_mtime > older_than:
                    w.rename(w.with_suffix(".dcm"))
                    n += 1
            except FileNotFoundError:
                continue
        return n


class SpoolWorker(threading.Thread):
    """Imports spooled objects; one savepoint per object, commit per batch."""

    def __init__(self, spool: Spool, maker: sessionmaker[Session], secret: bytes,
                 keep: bool = False, interval: float = 1.0, batch: int = 100,
                 notify=None, device_alerts: bool = False):
        super().__init__(name="dosimetrix-spool", daemon=True)
        self.spool, self.maker, self.secret = spool, maker, secret
        self.keep, self.interval, self.batch = keep, interval, batch
        self.notify = notify  # notify.NotifySettings: send alert notifications when idle
        self._last_dispatch = 0.0
        self.run_device_alerts = device_alerts  # the live receiver: hourly device rules
        self._last_devices = 0.0
        self.stop_event = threading.Event()
        self.stats: dict[str, int] = {}

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.drain():
                    self.device_alerts()
                    self.dispatch()
                    self.stop_event.wait(self.interval)
            except Exception:  # keep the worker alive; the object stays in incoming/
                log.exception("spool worker error")
                self.stop_event.wait(5)

    def stop(self) -> None:
        self.stop_event.set()

    def device_alerts(self, every: float = 3600.0) -> None:
        """Device rules (silent device, dose drift), at most every ``every`` seconds."""
        import time

        if not self.run_device_alerts or time.monotonic() - self._last_devices < every:
            return
        self._last_devices = time.monotonic()
        from ..alerts import evaluate_devices

        try:
            with self.maker() as session:
                stats = evaluate_devices(session)
                session.commit()
            if stats.get("new"):
                log.info("device alerts: %s", dict(stats))
        except Exception:  # noqa: BLE001 – alerts must never stop the importer
            log.exception("device alert evaluation failed")

    def dispatch(self, every: float = 30.0) -> None:
        """Send pending alert notifications, at most every ``every`` seconds."""
        import time

        if self.notify is None or time.monotonic() - self._last_dispatch < every:
            return
        self._last_dispatch = time.monotonic()
        from ..notify import dispatch

        try:
            with self.maker() as session:
                sent = dispatch(session, self.notify)
                session.commit()
            if sent:
                log.info("notifications: %s", dict(sent))
        except Exception:  # noqa: BLE001 – notifications must never stop the importer
            log.exception("notification dispatch failed")

    def drain(self) -> int:
        """Import what is pending now; return the number of files handled."""
        self.spool.recover()
        files = self.spool.pending()[: self.batch]
        if not files:
            return 0
        handled = 0
        with self.maker() as session:
            store = Store(session, self.secret, source="scp")
            for path in files:
                work = self.spool.claim(path)
                if work is None:
                    continue
                status, message = self._one(session, store, work)
                if status == "retry":  # put it back and try the batch again later
                    work.rename(work.with_suffix(".dcm"))
                    self.stats["retried"] = self.stats.get("retried", 0) + 1
                    log.warning("database error, will retry %s: %s", path.name, message)
                    session.rollback()
                    break
                self.stats[status] = self.stats.get(status, 0) + 1
                self._dispose(work, status, message)
                handled += 1
            try:
                session.commit()
            except OperationalError as e:
                log.warning("commit failed, batch will be retried: %s", e)
                session.rollback()
        return handled

    def _one(self, session: Session, store: Store, path: Path) -> tuple[str, str]:
        try:
            ds = read(path)
            if "SOPInstanceUID" not in ds or "SOPClassUID" not in ds:
                return "failed", "not a DICOM object (no SOP Instance/Class UID)"
            with session.begin_nested():
                r = store.store(parse(ds))
            return r.status, r.message
        except OperationalError as e:  # database unavailable/locked: not the object's fault
            return "retry", f"{type(e).__name__}: {e}"
        except IntegrityError as e:  # a concurrent import created the same row: try again
            return "retry", f"{type(e).__name__}: {e}"
        except Exception as e:
            return "failed", f"{type(e).__name__}: {e}"

    def _dispose(self, path: Path, status: str, message: str) -> None:
        name = path.with_suffix(".dcm").name
        if status == "failed":
            target = self.spool.failed / name
            shutil.move(path, target)
            target.with_suffix(".reason").write_text(message + "\n")
            log.warning("failed %s: %s", path.name, message)
        elif self.keep:
            day = self.spool.archive / dt.date.today().strftime("%Y%m%d")
            day.mkdir(exist_ok=True)
            shutil.move(path, day / name)
        else:
            path.unlink(missing_ok=True)


def wait_until_empty(spool: Spool, timeout: float = 30) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not spool.pending():
            return True
        time.sleep(0.1)
    return False
