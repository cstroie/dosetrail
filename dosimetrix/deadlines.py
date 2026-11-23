"""Reporting deadlines of Order MS 1255/2016, shown on the overview as the date approaches.

* art. 8(3): the quarterly centralised report, to the county public-health authority (LIR-DSP) by
  the **20th of the month after the quarter**;
* art. 12(4): the annual DRL return (20 patients per examination and device, February and March)
  by **15 April**.

Nothing is sent automatically: a deadline is shown from three weeks before until two months after,
and a person marks it as sent (stored in ``app_setting``, audited). An item has an id such as
``insp:2026Q3`` or ``art12:2026``.
"""

from __future__ import annotations

import datetime as dt

LEAD_DAYS = 21
GRACE_DAYS = 60
DONE_KEY = "deadlines.done"


def _quarter_due(year: int, quarter: int) -> dt.date:
    month = 3 * quarter + 1
    return dt.date(year + (month > 12), month - 12 if month > 12 else month, 20)


def upcoming(today: dt.date, done: set[str] | list[str] = ()) -> list[dict]:
    """The deadlines in their window that are not marked done, the most urgent first."""
    done = set(done)
    items: list[tuple[str, str, dict, dt.date]] = []
    for year in (today.year - 1, today.year):
        for q in (1, 2, 3, 4):
            items.append((f"insp:{year}Q{q}", "insp", {"year": year, "quarter": q},
                          _quarter_due(year, q)))
        items.append((f"art12:{year}", "art12", {"year": year}, dt.date(year, 4, 15)))
    out = []
    for id_, kind, period, due in items:
        left = (due - today).days
        if id_ in done or left > LEAD_DAYS or left < -GRACE_DAYS:
            continue
        out.append({"id": id_, "kind": kind, **period, "due": due.isoformat(), "days_left": left,
                    "level": "alert" if left < 0 else "warn" if left <= 7 else "info"})
    return sorted(out, key=lambda x: x["due"])
