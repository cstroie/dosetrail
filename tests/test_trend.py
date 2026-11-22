"""DRL trend over quarters."""

import datetime as dt

import pytest

from dosimetrix import classify
from dosimetrix.db.models import Event, Study
from dosimetrix.reports import trend
from dosimetrix.seeds.loader import load_all

AGE_2Y = int(2 * 365.25) + 30


def test_quarters_back_and_default_end():
    assert trend.quarters_back(2026, 2, 3) == [(2025, 4), (2026, 1), (2026, 2)]
    assert trend.default_end(dt.date(2026, 2, 10)) == (2025, 4)
    assert trend.default_end(dt.date(2026, 11, 21)) == (2026, 3)


def test_quarterly_means_against_drl(session):
    load_all(session)
    # CT head 1-4 y (national DRL 800 mGy.cm per series): rising over three quarters
    for i, (when, dlp) in enumerate([(dt.datetime(2026, 2, 1), 600.0),
                                     (dt.datetime(2026, 5, 1), 900.0),
                                     (dt.datetime(2026, 8, 1), 1000.0)]):
        s = Study(study_uid=f"t.{i}", modality="CT", study_datetime=when, source_kind="test",
                  study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)", age_days=AGE_2Y)
        s.events = [Event(protocol="Head", dlp=dlp)]
        session.add(s)
    session.flush()
    classify.run(session)
    r = trend.build(session, [], 2026, 3, n=3)
    assert r["quarters"] == ["2026-T1", "2026-T2", "2026-T3"]
    (row,) = [x for x in r["rows"] if (x["exam"], x["band"]) == ("CT-CAP", "1-4y")]
    assert [q["mean"] for q in row["quarters"]] == [600.0, 900.0, 1000.0]
    assert row["quarters_above"] == 2 and row["last_above"] is True
    assert row["trend_pct_per_quarter"] == pytest.approx(25.0)  # +200 mGy.cm / quarter of 800
