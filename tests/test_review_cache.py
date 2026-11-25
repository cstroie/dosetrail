"""Admin → Protocol mappings reads its counts from storage; the maintenance and decisions keep it right."""

from __future__ import annotations

import datetime as dt

import pytest

from dozimetrix import classify
from dozimetrix.api import review
from dozimetrix.db.migrate import upgrade
from dozimetrix.db.models import AppSetting, Device, Event, Study
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.seeds.loader import load_all

NOW = dt.datetime(2026, 11, 20, 12, 0)


@pytest.fixture()
def session(tmp_path):
    url = f"sqlite:///{tmp_path / 'r.sqlite3'}"
    upgrade(url)
    with make_sessionmaker(make_engine(url))() as s:
        load_all(s)
        dev = Device(display_name="Ysio", identity_key="k", is_local=True)
        s.add(dev)
        s.flush()
        for i, proto in enumerate(["W035 Torace a.p. 10kg", "ODD-ONE", "ODD-ONE"]):
            st = Study(study_uid=f"1.{i}", accession=f"A{i}", modality="DX", device_id=dev.id,
                       study_datetime=NOW, n_events=1, source_kind="test")
            st.events = [Event(protocol=proto, dap=0.1)]
            s.add(st)
        s.flush()
        classify.run(s)
        s.commit()
        yield s


def test_summary_is_stored_and_served_until_it_expires(session):
    first = review.cached_summary(session)
    rx = next(r for r in first if r["family"] == "rx")
    assert rx["studies"] == 3 and rx["counted_at"]
    session.add(Study(study_uid="9", accession="N", modality="DX", device_id=1, n_events=0,
                      study_datetime=NOW, source_kind="test"))
    session.flush()
    again = {r["family"]: r for r in review.cached_summary(session)}
    assert again["rx"]["studies"] == 3  # served from storage: the new study is not counted yet
    assert {r["family"]: r for r in review.summary(session)}["rx"]["studies"] == 4  # the live count
    row = session.get(AppSetting, review.SUMMARY_KEY)  # an old count is recomputed
    row.value = {**row.value, "at": (dt.datetime.now() - dt.timedelta(days=2)).isoformat()}
    session.flush()
    assert {r["family"]: r for r in review.cached_summary(session)}["rx"]["studies"] == 4


def test_unclassified_list_is_stored_and_a_decision_edits_it(session):
    items = review.cached_unclassified(session, "rx")
    assert [i["texts"]["protocol"] for i in items] == ["ODD-ONE"] and items[0]["exposures"] == 2
    assert session.get(AppSetting, review.TODO_KEY + "rx") is not None
    review.decide(session, "rx", "protocol", "odd-one", "RG-CVLM")  # case does not matter
    assert review.cached_unclassified(session, "rx") == []
    review.decide(session, "rx", "protocol", "odd-one", None)  # clearing: recounted on the next read
    assert session.get(AppSetting, review.TODO_KEY + "rx") is None
    assert review.cached_unclassified(session, "rx") == review.unclassified(session, "rx")  # the live count


def test_refresh_recounts_everything_and_the_limit_applies(session):
    out = review.refresh(session)
    assert set(out["families"]) == set(review.FAMILIES) and out["families"]["rx"] == 1
    assert review.cached_unclassified(session, "rx", limit=0) == []
    with pytest.raises(review.ReviewError):
        review.cached_unclassified(session, "nope")
