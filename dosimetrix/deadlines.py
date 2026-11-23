"""Reporting deadlines, shown on the overview as the date approaches.

They come from the specs: a spec with a ``[deadline]`` block is due on a date its authority fixed,
and every spec dropped in the specs folder with one appears here with no code. Bundled today:

* Order MS 1255/2016 art. 8(3): the quarterly centralised report, by the **20th of the month after
  the quarter** (``ro-insp-1255-quarterly``);
* art. 12(4): the annual DRL return, by **15 April** (``ro-insp-1255-art12``).

    [deadline]
    id = "insp"                   # prefix of the item ids (``insp:2026Q3``) and the key
    months_after_period = 1       # due in the Nth month after the period ends ...
    day = 20                      #   ... on this day
    # or, for a yearly spec, an absolute date in the period's year:
    # month = 4  day = 15
    # lead_days = 21  grace_days = 60      (optional, these are the defaults)

Nothing is sent automatically: a deadline is shown from ``lead_days`` before until ``grace_days``
after, and a person marks it as sent (stored in ``app_setting``, audited).
"""

from __future__ import annotations

import datetime as dt

LEAD_DAYS = 21
GRACE_DAYS = 60
DONE_KEY = "deadlines.done"


def _quarter_due(year: int, quarter: int, months_after: int = 1, day: int = 20) -> dt.date:
    """The ``day`` of the month ``months_after`` months after the quarter ends."""
    month = 3 * quarter + months_after
    return dt.date(year + (month - 1) // 12, (month - 1) % 12 + 1, day)


def _items(spec, today: dt.date) -> list[tuple[str, dict, dt.date]]:
    """(item id, period, due date) of one spec in the year before and the current one."""
    dl = spec.deadline
    kind = (spec.params.get("period") or {}).get("type")
    after, day = int(dl.get("months_after_period", 1)), int(dl["day"])
    out = []
    for year in (today.year - 1, today.year):
        if kind == "quarter":
            for q in (1, 2, 3, 4):
                out.append((f"{dl['id']}:{year}Q{q}", {"year": year, "quarter": q},
                            _quarter_due(year, q, after, day)))
        elif kind == "year":
            due = (dt.date(year, int(dl["month"]), day) if "month" in dl
                   else dt.date(year + (after - 1) // 12 + 1, (after - 1) % 12 + 1, day))
            out.append((f"{dl['id']}:{year}", {"year": year}, due))
        elif kind == "month":
            for m in range(1, 13):
                month = m + after
                out.append((f"{dl['id']}:{year}-{m:02d}", {"month": f"{year}-{m:02d}"},
                            dt.date(year + (month - 1) // 12, (month - 1) % 12 + 1, day)))
    return out


def _bundled():
    from .specs import loader

    return list(loader.discover(None).specs.values())


def upcoming(today: dt.date, done: set[str] | list[str] = (), specs=None) -> list[dict]:
    """The deadlines in their window that are not marked done, the most urgent first.
    ``specs``: the registered specs (default: the bundled ones)."""
    done = set(done)
    out = []
    for spec in (specs if specs is not None else _bundled()):
        if not spec.deadline:
            continue
        lead = int(spec.deadline.get("lead_days", LEAD_DAYS))
        grace = int(spec.deadline.get("grace_days", GRACE_DAYS))
        for id_, period, due in _items(spec, today):
            left = (due - today).days
            if id_ in done or left > lead or left < -grace:
                continue
            out.append({"id": id_, "kind": spec.deadline["id"], "spec": spec.id, **period,
                        "title": spec.title, "legal": spec.legal, "due": due.isoformat(),
                        "days_left": left,
                        "level": "alert" if left < 0 else "warn" if left <= 7 else "info"})
    return sorted(out, key=lambda x: x["due"])
