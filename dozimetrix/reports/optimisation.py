"""Before / after comparison of an optimisation: the doses of the period before a protocol change
against the period after it, per exam and patient group, with a significance test.

For the annual local DRL review: a protocol was changed on a date (a lower reference mAs, a new
iterative reconstruction, a different filter); did the dose drop, by how much, and is it more than
chance? The values are those of the DRL comparison (same exams, groups, quantities and
exclusions), so the rows line up with the DRL page. Per row: n, median and P75 before and after,
the change of the median, the ratio to the DRL before and after, and the Mann-Whitney U test
(two-sided, normal approximation with tie correction and continuity correction; fine from about
10 values a side).

The test says the dose changed, not that the images are still good: dose goes down for free by
degrading the image, so the report carries the image-quality note the reviewer wrote (reader
scores, noise measurements, the radiologists' feedback) next to the numbers, and a row that is
significantly lower is "lower", never "better".
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from collections import Counter
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Device, GroupBand, GroupScheme, ReferenceSet, ReferenceValue, Study
from ..drl import QUANTITY, DRLError, Item, collect
from ..stats import describe

ALPHA = 0.05
MIN_CHANGE = 0.05  # a median change under 5 % is "no change", whatever the p value


def mann_whitney(a: list[float], b: list[float]) -> tuple[float, float]:
    """(U of a, two-sided p) by the normal approximation with tie and continuity corrections."""
    n1, n2 = len(a), len(b)
    if not n1 or not n2:
        return 0.0, 1.0
    pooled = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks, ties = [0.0] * len(pooled), 0.0
    i = 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        ties += t**3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, pooled, strict=True) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return u1, 1.0
    z = (abs(u1 - n1 * n2 / 2) - 0.5) / math.sqrt(var)
    return u1, math.erfc(max(z, 0.0) / math.sqrt(2))


def _verdict(n_b: int, n_a: int, min_n: int, change: float | None, p: float | None) -> str:
    if n_b < min_n or n_a < min_n or change is None:
        return "insufficient"
    if p is not None and p < ALPHA and abs(change) >= MIN_CHANGE:
        return "lower" if change < 0 else "higher"
    return "no_change"


def build(session: Session, conds: list, change: dt.date, *, days: int = 180,
          set_key: str = "ro-ms838-2025", min_n: int = 10, exam: str | None = None,
          note: str | None = None) -> dict:
    """Doses in the ``days`` before ``change`` against the ``days`` from it, among the studies
    ``conds`` selects (no period in conds: the two windows are the period)."""
    rs = session.scalar(select(ReferenceSet).filter_by(key=set_key))
    if rs is None:
        raise DRLError(f"unknown reference set {set_key!r}")
    if not 7 <= days <= 1500:
        raise DRLError("days is 7..1500")
    t0 = dt.datetime.combine(change, dt.time())
    windows = {"before": [Study.study_datetime >= t0 - dt.timedelta(days=days),
                          Study.study_datetime < t0],
               "after": [Study.study_datetime >= t0,
                         Study.study_datetime < t0 + dt.timedelta(days=days)]}
    from ..db.models import Code

    refs = session.execute(
        select(ReferenceValue, Code, GroupBand, GroupScheme)
        .join(Code, Code.id == ReferenceValue.exam_code_id)
        .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
        .outerjoin(GroupScheme, GroupScheme.id == GroupBand.scheme_id)
        .where(ReferenceValue.set_id == rs.id)).all()
    refs = [r for r in refs if r[0].quantity in QUANTITY
            and QUANTITY[r[0].quantity][1].get(r[0].unit) is not None
            and (exam is None or r[1].code == exam)]
    physical = {i: (m or i) for i, m in session.execute(select(Device.id, Device.merged_into_id))}
    items = [Item(key=i, exam_code_id=rv.exam_code_id, quantity=rv.quantity, unit=rv.unit,
                  applies_to=rv.applies_to, scheme=scheme.key if scheme else None,
                  parameter=scheme.parameter if scheme else None,
                  band=band.key if band else None, projection=rv.projection)
             for i, (rv, _c, band, scheme) in enumerate(refs)]
    got = {name: collect(session, [*conds, *w], items, physical=physical)
           for name, w in windows.items()}
    rows = []
    for i, (rv, code, band, _scheme) in enumerate(refs):
        b = got["before"].get(i, {}).get(None, [])
        a = got["after"].get(i, {}).get(None, [])
        if not b and not a:
            continue
        db, da = describe(b), describe(a)
        change_pct = (da["median"] / db["median"] - 1) if db["n"] and da["n"] \
            and db["median"] else None
        p = mann_whitney(b, a)[1] if len(b) >= 2 and len(a) >= 2 else None
        rows.append({"exam": code.code, "band": band.label if band else None,
                     "quantity": rv.quantity, "unit": rv.unit, "projection": rv.projection,
                     "reference": rv.value, "n_before": len(b), "n_after": len(a),
                     "median_before": db.get("median"), "median_after": da.get("median"),
                     "p75_before": db.get("p75"), "p75_after": da.get("p75"),
                     "change": change_pct, "p": p,
                     "ratio_before": db["median"] / rv.value if db["n"] and rv.value else None,
                     "ratio_after": da["median"] / rv.value if da["n"] and rv.value else None,
                     "verdict": _verdict(len(b), len(a), min_n, change_pct, p)})
    rows.sort(key=lambda r: (r["exam"], r["quantity"], r["projection"] or "", r["band"] or ""))
    return {"set": rs.key, "change": change.isoformat(), "days": days, "min_n": min_n,
            "before": [(t0 - dt.timedelta(days=days)).date().isoformat(), change.isoformat()],
            "after": [change.isoformat(), (t0 + dt.timedelta(days=days)).date().isoformat()],
            "image_quality_note": (note or "").strip() or None,
            "summary": dict(Counter(r["verdict"] for r in rows)), "rows": rows}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["change_date", rep["change"], "before", " – ".join(rep["before"]), "after",
                " – ".join(rep["after"])])
    w.writerow(["image_quality_note", rep["image_quality_note"] or "(none recorded)"])
    w.writerow([])
    w.writerow(["exam", "group", "quantity", "unit", "projection", "reference", "n_before",
                "n_after", "median_before", "median_after", "p75_before", "p75_after",
                "change_pct", "p", "ratio_before", "ratio_after", "verdict"])

    def r(v, d=3):
        return None if v is None else round(v, d)

    for x in rep["rows"]:
        w.writerow([x["exam"], x["band"], x["quantity"], x["unit"], x["projection"],
                    x["reference"], x["n_before"], x["n_after"], r(x["median_before"]),
                    r(x["median_after"]), r(x["p75_before"]), r(x["p75_after"]),
                    r(None if x["change"] is None else 100 * x["change"], 1), r(x["p"], 4),
                    r(x["ratio_before"], 2), r(x["ratio_after"], 2), x["verdict"]])
    text.detach()
