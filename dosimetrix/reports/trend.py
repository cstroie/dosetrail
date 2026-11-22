"""DRL trend over the last quarters (Order MS 1245/2021 art. 8(2); the INSP example analysis
is semestrial, over 6 quarterly means).

For every exam × age group × quantity of a reference set (national DRLs by default):
* per quarter: n, mean, median, and mean / DRL;
* the number of quarters whose mean exceeds the DRL, and whether the last one does;
* the direction over the period: the least-squares slope of the quarterly means, as % of
  the DRL per quarter.

Values are the same units the DRL comparison uses (CT DLP per series, radiography DAP per
projection with retakes, fluoroscopy per study). Studies with a DRL exclusion reason are
left out.
"""

from __future__ import annotations

import datetime as dt
from typing import IO

from sqlalchemy.orm import Session

from ..db.models import Study
from ..drl import compare
from ..export.xlsx import XlsxWriter
from .insp1255 import quarter_range


def quarters_back(year: int, quarter: int, n: int) -> list[tuple[int, int]]:
    out = []
    y, q = year, quarter
    for _ in range(n):
        out.append((y, q))
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    return list(reversed(out))


def _slope(points: list[tuple[int, float]]) -> float | None:
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / den if den else None


def build(session: Session, conds: list, year: int, quarter: int, n: int = 6,
          set_key: str = "ro-ms838-2025") -> dict:
    qs = quarters_back(year, quarter, n)
    rows: dict[tuple, dict] = {}
    name = source = None
    for i, (y, q) in enumerate(qs):
        start, end = quarter_range(y, q)
        res = compare(session, [*conds, Study.study_datetime >= start,
                                Study.study_datetime < end], set_key, min_n=1)
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
        slope = _slope([(i, x["mean"]) for i, x in filled])
        out.append({**row, "quarters_with_data": len(filled), "quarters_above": len(above),
                    "last_above": bool(last and last["mean"] > row["reference"]),
                    "trend_pct_per_quarter": (100 * slope / row["reference"]
                                              if slope is not None else None)})
    out.sort(key=lambda r: (-r["quarters_above"], r["exam"], r["band"] or ""))
    return {"set": set_key, "name": name, "source": source,
            "quarters": [f"{y}-T{q}" for y, q in qs], "rows": out}


def write_xlsx(report: dict, target: IO, info: dict[str, object]) -> None:
    qn = report["quarters"]
    head = ["Examinare", "Cod", "Grupa de vârstă", "Mărime", "NRD", "Unitate"]
    for q in qn:
        head += [f"{q} n", f"{q} medie", f"{q} medie/NRD", f"{q} mediană/NRD"]
    head += ["Trimestre peste NRD", "Ultimul trimestru peste NRD", "Tendință (% NRD/trim.)"]
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [30, 80])
        for k, v in [("Raport", "Analiza semestrială a dozelor medii trimestriale față de "
                                "NRD (OMS 1245/2021, art. 8(2))"),
                     ("Set de referință", report["name"]), ("Sursa", report["source"]),
                     ("Trimestre", ", ".join(qn)), *info.items(),
                     ("Notă", "CT: DLP pe secvență; radiografie: DAP al incidenței (cu "
                              "repetări); radioscopie: DAP al studiului. Media trimestrială "
                              "este comparată cu NRD; mediana arată dacă media e trasă de "
                              "câteva valori mari.")]:
            about.row([k, v])
        ws = wb.sheet("Tendinte", head, [30, 16, 12, 8, 8, 8] + [8, 10, 10, 10] * len(qn)
                      + [10, 10, 12])
        for r in report["rows"]:
            cells = [r["exam_meaning"], r["exam"], r["band_label"], r["quantity"],
                     r["reference"], r["unit"]]
            for x in r["quarters"]:
                cells += ([x["n"], round(x["mean"], 4), round(x["mean_over_drl"], 3),
                           round(x["median"] / r["reference"], 3)]
                          if x else [None, None, None, None])
            cells += [r["quarters_above"], "da" if r["last_above"] else "nu",
                      round(r["trend_pct_per_quarter"], 1)
                      if r["trend_pct_per_quarter"] is not None else None]
            ws.row(cells)


def default_end(today: dt.date | None = None) -> tuple[int, int]:
    """The last complete quarter."""
    today = today or dt.date.today()
    q = (today.month - 1) // 3
    return (today.year - 1, 4) if q == 0 else (today.year, q)
