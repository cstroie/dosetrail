"""``series``: a reference comparison repeated over the last N periods, with the direction it moves.

The semestrial analysis of Order MS 1245/2021 art. 8(2) (the INSP example is over 6 quarterly
means): for every exam x age group x quantity of a reference set, per quarter the n, mean and
median and mean / DRL; how many quarters the mean exceeded the DRL, whether the last one did, and
the least-squares slope of the quarterly means as % of the DRL per quarter.

    [series]
    reference_set = "ro-ms838-2025"       # a default; the ``set`` parameter overrides it
    periods = "${rules.quarters}"          # how many quarters back (the chosen one included)
    min_n = "${rules.min_n_per_quarter}"   # values a quarter's mean needs
    sheet = "Tendinte"
    note = "..."                           # the last information line
"""

from __future__ import annotations

import datetime as dt

from ...db.models import Study
from ...drl import compare
from ..context import Context
from ..loader import _quarter
from ..registry import kind
from ..result import Book, Result, Sheet


def quarters_back(year: int, quarter: int, n: int) -> list[tuple[int, int]]:
    out = []
    y, q = year, quarter
    for _ in range(n):
        out.append((y, q))
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    return list(reversed(out))


def slope(points: list[tuple[int, float]]) -> float | None:
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / den if den else None


def default_end(today: dt.date | None = None) -> tuple[int, int]:
    """The last complete quarter."""
    today = today or dt.date.today()
    q = (today.month - 1) // 3
    return (today.year - 1, 4) if q == 0 else (today.year, q)


def build_report(ctx: Context) -> dict:
    body = ctx.spec.body["series"]
    n = int(ctx.params.get("quarters") or body["periods"])
    set_key = ctx.params.get("set") or body["reference_set"]
    year, quarter = int(ctx.params["year"]), int(ctx.params["quarter"])
    min_n = body.get("min_n", 1)
    qs = quarters_back(year, quarter, n)
    rows: dict[tuple, dict] = {}
    name = source = None
    for i, (y, q) in enumerate(qs):
        start, end = _quarter(y, q)
        res = compare(ctx.session, [*ctx.conds, Study.study_datetime >= start,
                                    Study.study_datetime < end], set_key, min_n=min_n)
        name, source = res["name"], res["source"]
        for r in res["rows"]:
            if r["status"] == "unavailable":
                continue
            key = (r["exam"], r["band"], r["quantity"])
            row = rows.setdefault(key, {
                "exam": r["exam"], "exam_meaning": r["exam_meaning"], "band": r["band"],
                "band_label": r["band_label"], "quantity": r["quantity"],
                "applies_to": r["applies_to"], "reference": r["reference"], "unit": r["unit"],
                "quarters": [None] * len(qs)})
            if r["groups"]:
                g = r["groups"][0]
                row["quarters"][i] = {"n": g["n"], "mean": g["mean"], "median": g["median"],
                                      "mean_over_drl": g["mean"] / r["reference"]}
    out = []
    for row in rows.values():
        filled = [(i, x) for i, x in enumerate(row["quarters"]) if x]
        if not filled:
            continue
        above = [x for _, x in filled if x["mean"] > row["reference"]]
        last = row["quarters"][-1]
        sl = slope([(i, x["mean"]) for i, x in filled])
        out.append({**row, "quarters_with_data": len(filled), "quarters_above": len(above),
                    "last_above": bool(last and last["mean"] > row["reference"]),
                    "trend_pct_per_quarter": (100 * sl / row["reference"]
                                              if sl is not None else None)})
    out.sort(key=lambda r: (-r["quarters_above"], r["exam"], r["band"] or ""))
    return {"set": set_key, "name": name, "source": source,
            "quarters": [f"{y}-T{q}" for y, q in qs], "rows": out}


def to_result(ctx: Context, report: dict, info: dict | None = None) -> Result:
    body = ctx.spec.body["series"]
    qn = report["quarters"]
    head = ["Examinare", "Cod", "Grupa de vârstă", "Mărime", "NRD", "Unitate"]
    for q in qn:
        head += [f"{q} n", f"{q} medie", f"{q} medie/NRD", f"{q} mediană/NRD"]
    head += ["Trimestre peste NRD", "Ultimul trimestru peste NRD", "Tendință (% NRD/trim.)"]
    lines = [("Raport", body.get("title_ro", ctx.spec.label("ro"))),
             ("Set de referință", report["name"]), ("Sursa", report["source"]),
             ("Trimestre", ", ".join(qn)), *(info or {}).items(), ("Notă", body.get("note", ""))]
    rows = []
    for r in report["rows"]:
        cells = [r["exam_meaning"], r["exam"], r["band_label"], r["quantity"], r["reference"],
                 r["unit"]]
        for x in r["quarters"]:
            cells += ([x["n"], round(x["mean"], 4), round(x["mean_over_drl"], 3),
                       round(x["median"] / r["reference"], 3)] if x else [None] * 4)
        cells += [r["quarters_above"], "da" if r["last_above"] else "nu",
                  round(r["trend_pct_per_quarter"], 1)
                  if r["trend_pct_per_quarter"] is not None else None]
        rows.append(cells)
    widths = [30, 16, 12, 8, 8, 8] + [8, 10, 10, 10] * len(qn) + [10, 10, 12]
    sheet = Sheet(body.get("sheet", "Tendinte"), head, rows, widths)
    return Result([Book(f"{ctx.spec.id}.xlsx", [sheet], ctx.spec.label("ro"), lines)],
                  extra={"report": report})


class Series:
    identifying = False

    @staticmethod
    def validate(spec) -> list[str]:
        body = spec.body.get("series")
        if not isinstance(body, dict):
            return ["series needs a [series] table"]
        errs = []
        for k in ("reference_set", "periods"):
            if body.get(k) in (None, ""):
                errs.append(f"[series] needs {k}")
        if (spec.params.get("period") or {}).get("type") != "quarter":
            errs.append("a series spec needs params.period.type = quarter")
        return errs

    @staticmethod
    def run(ctx: Context) -> Result:
        return to_result(ctx, build_report(ctx), {
            "Generat": ctx.now.isoformat(sep=" ", timespec="seconds"), "De": ctx.username,
            "Selecție": ctx.params.get("q") or "(toate)"})


kind("series")(Series)
