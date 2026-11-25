"""Maintenance jobs: one at a time under a lock, their state stored, started from the web."""

from __future__ import annotations

import datetime as dt

import pytest

from dozimetrix import jobs
from dozimetrix.db.models import AppSetting
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.db.migrate import upgrade


@pytest.fixture()
def maker(tmp_path):
    url = f"sqlite:///{tmp_path / 'jobs.sqlite3'}"
    upgrade(url)
    return make_sessionmaker(make_engine(url))


def state(maker):
    with maker() as s:
        return dict(s.get(AppSetting, jobs.JOB_KEY).value)


def test_execute_records_state_and_result(maker, tmp_path):
    lock = tmp_path / "x.lock"

    def run(say):
        say("one")
        say("two")
        return {"ok": True, "n": 3}

    out = jobs.execute(maker, "classify", run, {"days": 7}, "ann", lock, say=lambda m: None)
    assert out == {"ok": True, "n": 3}
    st = state(maker)
    assert (st["job"], st["state"], st["by"], st["options"]) == ("classify", "done", "ann", {"days": 7})
    assert st["lines"] == ["one", "two"] and st["result"]["n"] == 3 and st["finished"]
    with maker() as s:
        assert jobs.current(s, lock)["state"] == "done"


def test_a_failed_result_or_exception_is_recorded(maker, tmp_path):
    lock = tmp_path / "x.lock"
    jobs.execute(maker, "maintain", lambda say: {"ok": False}, {}, "ann", lock, say=lambda m: None)
    assert state(maker)["state"] == "failed"

    def boom(say):
        raise ValueError("no way")

    with pytest.raises(ValueError):
        jobs.execute(maker, "maintain", boom, {}, "ann", lock, say=lambda m: None)
    st = state(maker)
    assert st["state"] == "failed" and "no way" in st["error"]
    assert not jobs.is_busy(lock)  # the lock is released after a failure


def test_only_one_job_at_a_time(maker, tmp_path):
    lock = tmp_path / "x.lock"
    with jobs.locked(lock):
        assert jobs.is_busy(lock)
        with pytest.raises(jobs.Busy):
            jobs.execute(maker, "maintain", lambda say: {}, {}, "ann", lock)
    assert not jobs.is_busy(lock)


def test_a_running_job_without_a_lock_shows_interrupted(maker, tmp_path):
    lock = tmp_path / "x.lock"
    old = (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    with maker() as s:
        s.add(AppSetting(key=jobs.JOB_KEY, value={"job": "maintain", "state": "running", "started": old}))
        s.commit()
    with maker() as s:
        assert jobs.current(s, lock)["state"] == "interrupted"
    with jobs.locked(lock), maker() as s:  # the lock is held: it really runs
        assert jobs.current(s, lock)["state"] == "running"
    fresh = dt.datetime.now().isoformat(timespec="seconds")  # just started from the web: grace
    with maker() as s:
        s.get(AppSetting, jobs.JOB_KEY).value = {"job": "maintain", "state": "running", "started": fresh}
        s.commit()
    with maker() as s:
        assert jobs.current(s, lock)["state"] == "running"


def test_command_lines():
    assert jobs.argv("maintain", 30, "ann") == ["maintain", "--days", "30", "--by", "ann"]
    assert jobs.argv("maintain", None, "ann") == ["maintain", "--full", "--by", "ann"]
    assert jobs.argv("classify", None, "ann") == ["classify", "--by", "ann"]
    assert jobs.argv("classify", 5, "ann") == ["classify", "--days", "5", "--by", "ann"]
    assert jobs.argv("recompute", 5, "ann") == ["recompute", "--by", "ann"]
