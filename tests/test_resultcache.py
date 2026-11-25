"""The short-lived cache of heavy aggregate answers."""

from __future__ import annotations

import datetime as dt
import threading
import time

import pytest

from dozimetrix.api.resultcache import ResultCache, weight
from dozimetrix.db.migrate import upgrade
from dozimetrix.db.models import AuditLog, Study
from dozimetrix.db.session import make_engine, make_sessionmaker


@pytest.fixture()
def session(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.sqlite3'}"
    upgrade(url)
    with make_sessionmaker(make_engine(url))() as s:
        yield s


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def study(uid):
    return Study(study_uid=uid, accession=uid, modality="DX", study_datetime=dt.datetime(2026, 1, 1),
                 source_kind="test")


def test_second_call_is_served_from_the_cache(session):
    rc, calls = ResultCache(60), []
    compute = lambda: calls.append(1) or {"rows": [1, 2]}  # noqa: E731
    a = rc.get(session, "x", ("q",), compute)
    b = rc.get(session, "x", ("q",), compute)
    assert a is b and len(calls) == 1 and (rc.hits, rc.misses) == (1, 1)
    rc.get(session, "x", ("other",), compute)  # another query: computed
    rc.get(session, "y", ("q",), compute)      # another answer: computed
    assert len(calls) == 3


def test_an_entry_expires(session):
    clock = Clock()
    rc, calls = ResultCache(60, clock=clock), []
    compute = lambda: calls.append(1) or []  # noqa: E731
    rc.get(session, "x", (), compute)
    clock.t = 59
    rc.get(session, "x", (), compute)
    assert len(calls) == 1
    clock.t = 61
    rc.get(session, "x", (), compute)
    assert len(calls) == 2


def test_an_audited_change_starts_afresh_but_a_new_study_does_not(session):
    rc, calls = ResultCache(60), []
    compute = lambda: calls.append(1) or []  # noqa: E731
    rc.get(session, "x", (), compute)
    session.add(study("1"))  # studies arrive all the time (and in bulk during a PACS sync)
    session.flush()
    rc.get(session, "x", (), compute)
    assert len(calls) == 1
    session.add(AuditLog(action="user.update", target="x"))
    session.flush()
    rc.get(session, "x", (), compute)
    assert len(calls) == 2
    rc.get(session, "x", (), compute)
    assert len(calls) == 2


def test_clear_and_off_switch(session):
    rc, calls = ResultCache(60), []
    compute = lambda: calls.append(1) or []  # noqa: E731
    rc.get(session, "x", (), compute)
    rc.clear()
    rc.get(session, "x", (), compute)
    assert len(calls) == 2
    off = ResultCache(0)
    off.get(session, "x", (), compute)
    off.get(session, "x", (), compute)
    assert len(calls) == 4


def test_an_error_is_not_cached(session):
    rc, calls = ResultCache(60), []

    def compute():
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("bad query")
        return "ok"

    with pytest.raises(ValueError):
        rc.get(session, "x", (), compute)
    assert rc.get(session, "x", (), compute) == "ok" and len(calls) == 2


def test_the_busiest_answers_are_kept_within_the_budget(session):
    rc = ResultCache(60, max_entries=3, max_weight=100)
    for i in range(5):
        rc.get(session, "x", (i,), lambda: [0] * 10)
    assert len(rc._items) == 3  # by entries
    rc.get(session, "big", (), lambda: [0] * 95)
    assert sum(w for _, _, w in rc._items.values()) <= 100  # by weight: the older ones went
    assert weight([1, 2]) == 3 and weight({"a": [1, 2, 3], "b": 5}) == 4 and weight(7) == 1


def test_simultaneous_callers_share_one_computation(tmp_path):
    url = f"sqlite:///{tmp_path / 't.sqlite3'}"
    upgrade(url)
    maker = make_sessionmaker(make_engine(url))
    rc, calls, out = ResultCache(60), [], []

    def compute():
        calls.append(1)
        time.sleep(0.3)
        return {"v": 1}

    def worker():
        with maker() as s:
            out.append(rc.get(s, "x", (), compute))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and len(out) == 4 and all(o is out[0] for o in out)
