"""A short-lived cache of the heavy aggregate answers (chart rows, DRL comparisons, statistics,
data quality, proposed local DRLs).

Each takes 0.4 – 3.5 s at 640k studies, is asked by every open chart page, and gives the same
answer to everyone for the same query (no result depends on who asks). The first caller computes it
and the others wait for that result instead of running the same queries again.

An entry is good for ``ttl`` seconds, and only until something is changed through the web UI (the
key carries the newest audit-log entry, and an audited action clears the cache). New studies, the
HIS adding a weight and the nightly reclassify are not watched: they show up within the ``ttl``.
(The newest study id is deliberately not part of the key: while the PACS sync imports days of old
studies it moves every few seconds and nothing would ever be reused.) ``ttl`` 0 switches the cache
off.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import AuditLog

MAX_ENTRIES = 48
MAX_WEIGHT = 400_000  # rows held in all entries together (a chart answer is tens of thousands)


def weight(value) -> int:
    """Roughly how much an answer holds: its rows."""
    if isinstance(value, list):
        return len(value) + 1
    if isinstance(value, dict):
        return 1 + sum(len(v) for v in value.values() if isinstance(v, list))
    return 1


class ResultCache:
    def __init__(self, ttl: float = 120.0, clock=time.monotonic,
                 max_entries: int = MAX_ENTRIES, max_weight: int = MAX_WEIGHT):
        self.ttl, self.clock = ttl, clock
        self.max_entries, self.max_weight = max_entries, max_weight
        self._items: OrderedDict[tuple, tuple[float, object, int]] = OrderedDict()
        self._guards: dict[tuple, threading.Lock] = {}
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    @staticmethod
    def version(session: Session) -> int | None:
        """The newest audit-log entry: any change made through the UI (or the command line) adds one."""
        return session.scalar(select(func.max(AuditLog.id)))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def _fresh(self, key: tuple):
        hit = self._items.get(key)
        if hit and self.clock() - hit[0] < self.ttl:
            self._items.move_to_end(key)
            return hit
        return None

    def get(self, session: Session, name: str, params: tuple, compute):
        """The answer for ``(name, params)`` at the current data version: the cached one, else
        ``compute()`` (once, however many ask at the same time)."""
        if self.ttl <= 0:
            return compute()
        key = (name, params, self.version(session))
        with self._lock:
            hit = self._fresh(key)
            if hit:
                self.hits += 1
                return hit[1]
            guard = self._guards.setdefault(key, threading.Lock())
        with guard:
            with self._lock:  # someone else may have finished while this one waited
                hit = self._fresh(key)
                if hit:
                    self.hits += 1
                    return hit[1]
            try:
                value = compute()  # an error is not cached: the next caller tries again
            except BaseException:
                with self._lock:
                    self._guards.pop(key, None)
                raise
            with self._lock:
                self.misses += 1
                self._items[key] = (self.clock(), value, weight(value))
                self._guards.pop(key, None)
                self._evict()
            return value

    def _evict(self) -> None:
        now = self.clock()
        for k in [k for k, (at, _v, _w) in self._items.items() if now - at >= self.ttl]:
            del self._items[k]
        while self._items and (len(self._items) > self.max_entries
                               or sum(w for _, _, w in self._items.values()) > self.max_weight):
            self._items.popitem(last=False)  # the least recently used
