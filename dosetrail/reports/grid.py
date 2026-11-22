"""Dose statistics on the second grid (ICRP 135 / RP 185 groups, extended), next to the
national one.

Rows: the national DRL examinations (OMS 838/2025 codes), so the report lines up with the
national grid; columns: the ICRP 135 group of the patient (head exams by age, body exams by
weight; see grid.py) instead of the national age groups 1–4, 5–9, 10–14, ≥15 years.

Values:
* CT: DLP and CTDIvol per scan series (localisers excluded);
* radiography: DAP per exposure;
* fluoroscopy: DAP of the study.

Dose statistics leave out the studies with a DRL exclusion (weight below WHO P5 or above
P95 for age, prematurity); they are counted in "excluded". Body exams without a weight are
in "Weight unknown". Where RP 185 has a European DRL for the exam and group, the median is
compared with it: below, above (up to 1.5×) or well above; fewer than ``min_n`` values is
"insufficient" (the national survey plan asks for 10 per group).
"""

from __future__ import annotations

import datetime as dt
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Code, GroupBand, ReferenceSet, ReferenceValue, Study
from ..export.xlsx import XlsxWriter
from ..grid import LABEL, Grid, crosswalk
from ..stats import compute

NATIONAL = "ro-drl-ms838-2025"
# (modality filter, metric, quantity in RP 185, unit)
PARTS = [
    (("CT",), "dlp", "dlp", "mGy.cm"),
    (("CT",), "ctdivol", "ctdivol", "mGy"),
    (("DX", "CR"), "dap", "dap", "Gy.cm2"),
    (("RF",), "dap_total", "dap", "Gy.cm2"),
]
# RP 185 band keys that differ from the extended scheme: "≥6 y" covers 6–<18 y (the
# extended ≥18 y group has no paediatric EDRL)
BAND_ALIAS = {"6y+": "6-18y"}
STATUS_RO = {"below": "sub EDRL", "above": "peste EDRL (≤1,5×)",
             "well_above": "peste 1,5 × EDRL", "insufficient": "n insuficient", None: ""}


def _edrl(session: Session) -> dict[tuple[str, str, str], float]:
    """(national exam code, band key, quantity) -> RP 185 value, through the crosswalk:
    a nomenclator code (and projections) mapped both to a national and an RP 185 code."""
    rs = session.scalar(select(ReferenceSet).filter_by(key="ec-rp185-2018"))
    if rs is None:
        return {}
    from ..drl import QUANTITY

    # to the local unit (Gy·cm², mGy·cm, mGy); RP 185 radiography DAP is in mGy·cm²
    values = {(c, BAND_ALIAS.get(b, b), rv.quantity):
              rv.value / QUANTITY[rv.quantity][1].get(rv.unit, 1.0)
              for rv, c, b in session.execute(
        select(ReferenceValue, Code.code, GroupBand.key)
        .join(Code, Code.id == ReferenceValue.exam_code_id)
        .join(GroupBand, GroupBand.id == ReferenceValue.band_id)
        .where(ReferenceValue.set_id == rs.id, ReferenceValue.quantity.in_(list(QUANTITY))))}
    xw = crosswalk()
    rp185 = {(r["from_system"], r["from_code"], r["views"]): r["to_code"]
             for r in xw if r["to_system"] == "ec-rp185"}
    nat_to_eu: dict[str, str] = {}
    for r in xw:
        if r["to_system"] == NATIONAL:
            eu = rp185.get((r["from_system"], r["from_code"], r["views"]))
            if eu:
                nat_to_eu.setdefault(r["to_code"], eu)
    return {(nat, band, q): v for nat, eu in nat_to_eu.items()
            for (c, band, q), v in values.items() if c == eu}


def _status(median: float, ref: float | None, n: int, min_n: int) -> str | None:
    if ref is None:
        return None
    if n < min_n:
        return "insufficient"
    r = median / ref
    return "below" if r <= 1 else "above" if r <= 1.5 else "well_above"


def build(session: Session, conds: list, start: dt.datetime, end: dt.datetime,
          min_n: int = 10) -> dict:
    grid = Grid(session)
    edrl = _edrl(session)
    period = [Study.study_datetime >= start, Study.study_datetime < end]
    rows = []
    for mods, metric, quantity, unit in PARTS:
        base = [*conds, *period, Study.modality.in_(mods)]
        every = compute(session, base, metric, ["exam", "ref_group"], system=NATIONAL)
        kept = compute(session, [*base, Study.drl_exclusion.is_(None)], metric,
                       ["exam", "ref_group"], system=NATIONAL)
        kept_by = {(g["key"]["exam"], g["key"]["ref_group"]): g for g in kept["groups"]}
        for g in every["groups"]:
            exam, group = g["key"]["exam"], g["key"]["ref_group"]
            if exam is None:
                continue
            k = kept_by.get((exam, group), {"n": 0})
            band = group.partition(":")[2]
            ref = edrl.get((exam, band, quantity))
            row = {"exam": exam, "exam_meaning": g["label"]["exam"],
                   "parameter": grid.parameter(NATIONAL, exam), "group": group,
                   "group_label": g["label"]["ref_group"], "order": grid.order.get(group, 999),
                   "quantity": quantity, "metric": metric, "unit": unit,
                   "n_all": g["n"], "n": k["n"], "n_studies": k.get("n_studies", 0),
                   "excluded": g["n"] - k["n"], "mean": k.get("mean"),
                   "median": k.get("median"), "p75": k.get("p75"), "edrl": ref,
                   "ratio": k["median"] / ref if ref and k.get("median") else None}
            row["status"] = _status(k["median"], ref, k["n"], min_n) if k["n"] else None
            rows.append(row)
    rows.sort(key=lambda r: (r["exam"], r["quantity"], r["order"]))
    n_body = sum(r["n_all"] for r in rows if r["parameter"] == "weight")
    n_body_unknown = sum(r["n_all"] for r in rows if r["group"] == "weight:?")
    return {"grid": LABEL, "start": start.date().isoformat(), "end": end.date().isoformat(),
            "min_n": min_n, "rows": rows,
            "notes": {"body_values": n_body, "body_weight_unknown": n_body_unknown}}


def write_xlsx(report: dict, target: IO, info: dict[str, object]) -> None:
    n = report["notes"]
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [34, 80])
        for k, v in [("Raport", "Statistici de doză pe grila ICRP 135 / RP 185 (extinsă), "
                                "alături de grila națională"),
                     ("Perioada", f"{report['start']} – {report['end']}"), *info.items(),
                     ("Grupe — cap", "după vârstă: 0–<3 luni, 3 luni–<1 an, 1–<6 ani, "
                                     "6–<18 ani, ≥18 ani"),
                     ("Grupe — corp", "după greutate: <5, 5–<15, 15–<30, 30–<50, 50–<80, "
                                      "80–<110, ≥110 kg"),
                     ("Limite", "intervale semi-deschise: la limita exactă, grupa superioară"),
                     ("Valori corp fără greutate", f"{n['body_weight_unknown']} din "
                                                   f"{n['body_values']}"),
                     ("Notă", "CT: DLP și CTDIvol pe secvență; radiografie: DAP pe expunere; "
                              "radioscopie: DAP al studiului. Statisticile exclud studiile "
                              "cu greutate sub P5 / peste P95 OMS pentru vârstă și "
                              "prematurii (coloana «excluse»). EDRL: RP 185, unde există. "
                              f"Sub {report['min_n']} valori: n insuficient.")]:
            about.row([k, v])
        head = ["Examinare", "Cod", "Parametru", "Grupa", "Mărime", "Unitate", "n", "Studii",
                "Excluse", "Medie", "Mediană", "P75", "EDRL RP 185", "Mediană/EDRL", "Stare"]
        for title, param in (("Cap (varsta)", "age"), ("Corp (greutate)", "weight")):
            ws = wb.sheet(title, head, [30, 18, 10, 16, 9, 9, 7, 7, 7, 10, 10, 10, 10, 10, 18])
            for r in report["rows"]:
                if r["parameter"] != param:
                    continue
                ws.row([r["exam_meaning"], r["exam"],
                        "vârstă" if param == "age" else "greutate", r["group_label"],
                        r["quantity"], r["unit"], r["n"], r["n_studies"], r["excluded"] or None,
                        _round(r["mean"]), _round(r["median"]), _round(r["p75"]), r["edrl"],
                        round(r["ratio"], 3) if r["ratio"] else None,
                        STATUS_RO[r["status"]]])


def _round(v):
    return None if v is None else round(v, 4) if v < 10 else round(v, 1)
