"""Reporting deadlines (Order 1255/2016 art. 8 and 12) and the plan of measures (art. 7(2))."""

from __future__ import annotations

import datetime as dt

from dosimetrix import deadlines


def D(s):
    return dt.date.fromisoformat(s)


def ids(today, done=()):
    return [d["id"] for d in deadlines.upcoming(D(today), done)]


def test_quarterly_report_is_due_on_the_20th_of_the_next_month():
    assert deadlines._quarter_due(2026, 1) == D("2026-04-20")
    assert deadlines._quarter_due(2026, 3) == D("2026-10-20")
    assert deadlines._quarter_due(2026, 4) == D("2027-01-20")  # the year turns


def test_window_is_three_weeks_before_to_two_months_after():
    assert "insp:2026Q3" not in ids("2026-09-28")  # 22 days before: too early
    assert "insp:2026Q3" in ids("2026-09-29")  # 21 days
    d = next(x for x in deadlines.upcoming(D("2026-10-15")) if x["id"] == "insp:2026Q3")
    assert d["days_left"] == 5 and d["level"] == "warn" and d["due"] == "2026-10-20"
    d = next(x for x in deadlines.upcoming(D("2026-10-25")) if x["id"] == "insp:2026Q3")
    assert d["days_left"] == -5 and d["level"] == "alert"  # overdue
    assert "insp:2026Q3" in ids("2026-12-19") and "insp:2026Q3" not in ids("2026-12-20")


def test_annual_return_is_due_on_15_april_and_done_items_disappear():
    assert "art12:2026" in ids("2026-04-01")
    assert next(x for x in deadlines.upcoming(D("2026-04-01")) if x["id"] == "art12:2026"
                )["days_left"] == 14
    assert "art12:2026" not in ids("2026-04-01", done=["art12:2026"])
    assert "art12:2026" not in ids("2026-03-01")


def test_most_urgent_first():
    got = deadlines.upcoming(D("2026-04-10"))
    assert [d["due"] for d in got] == sorted(d["due"] for d in got)
    assert got[0]["id"] == "art12:2026"  # the 15th comes before the 20th
