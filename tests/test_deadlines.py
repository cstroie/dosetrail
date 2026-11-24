"""Reporting deadlines (Order 1255/2016 art. 8 and 12) and the plan of measures (art. 7(2))."""

from __future__ import annotations

import datetime as dt

from dozimetrix import deadlines


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


# ------------------------------------------------------------------ from the specs


def _spec(tmp_path, name, period, deadline, sid="t-dl"):
    import textwrap

    from dozimetrix.specs import loader

    p = tmp_path / f"{name}.toml"
    p.write_text(textwrap.dedent(f"""
        [spec]
        id = "{sid}"
        kind = "records"
        version = "1"
        title = {{ en = "Test return", ro = "Raport de test" }}
        legal = "Test act"
        [params]
        period = {{ type = "{period}" }}
        [deadline]
        {deadline}
        [[sheet]]
        name = "S"
        [[sheet.column]]
        header = "Study"
        field = "study.id"
    """))
    return loader.parse(p)


def test_a_dropped_spec_with_a_deadline_appears_with_no_code(tmp_path):
    monthly = _spec(tmp_path, "m", "month",
                    'id = "mon"\nmonths_after_period = 1\nday = 5\ngrace_days = 10')
    got = deadlines.upcoming(D("2026-11-01"), (), [monthly])
    assert [d["id"] for d in got] == ["mon:2026-10"]  # October's return, due 5 November
    assert got[0]["due"] == "2026-11-05" and got[0]["spec"] == "t-dl"
    assert got[0]["title"]["ro"] == "Raport de test" and got[0]["legal"] == "Test act"
    yearly = _spec(tmp_path, "y", "year", 'id = "yr"\nmonths_after_period = 3\nday = 31\n'
                   "lead_days = 30\ngrace_days = 10", "t-y")
    assert deadlines.upcoming(D("2027-03-05"), (), [yearly])[0]["id"] == "yr:2026"  # 31 March 2027
    assert deadlines.upcoming(D("2027-03-05"), (), [yearly])[0]["due"] == "2027-03-31"
    assert deadlines.upcoming(D("2027-04-15"), (), [yearly]) == []  # past its 10 days of grace


def test_a_spec_without_a_deadline_adds_nothing_and_a_bad_one_is_refused(tmp_path):
    import pytest

    from dozimetrix.specs import loader

    p = tmp_path / "x.toml"
    p.write_text("[spec]\nid='x'\nkind='records'\nversion='1'\ntitle='x'\n[[sheet]]\nname='S'\n"
                 "[[sheet.column]]\nheader='a'\nfield='study.id'\n")
    assert deadlines.upcoming(D("2026-11-01"), (), [loader.parse(p)]) == []
    with pytest.raises(loader.SpecError, match="deadline"):
        _spec(tmp_path, "b", "range", 'id = "b"\nday = 3\nmonths_after_period = 1', "t-bad")
    with pytest.raises(loader.SpecError, match="deadline"):
        _spec(tmp_path, "c", "year", 'id = "c"\nday = 3', "t-bad2")
