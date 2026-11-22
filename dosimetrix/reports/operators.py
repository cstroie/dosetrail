"""Operator dose report: which operators (radiographers, technologists) give doses well above
their colleagues' for the same exam and age group.

For optimisation and training, not blame. Every value is compared with the median of the
whole department for its national exam code and age group, which takes the case mix out:
an operator who does many older children is not penalised. Per operator:

* ``median_ratio``: median of value / group median (1.0 = like everyone else);
* ``share_p95``: share of their values above the group's 95th percentile (5 % expected).

An operator is flagged with at least ``min_n`` values and a median ratio >= 1.3 or a P95
share >= 10 % (twice what chance gives). Values: CT DLP and fluoroscopy DAP per study,
radiography DAP per projection. The operator is DICOM Operators' Name
(0008,1070), which many devices leave empty: the report shows how often each room fills it.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import IO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Classification, Code, CodeSystem, Device, Event, Study
from ..export.xlsx import XlsxWriter
from ..stats import BandLookup

NATIONAL = "ro-drl-ms838-2025"
MIN_GROUP = 20  # values an exam and age group needs before anyone is compared with it
RATIO_FLAG, P95_FLAG = 1.3, 0.10


def operator(name: str | None) -> str | None:
    """One spelling per person: DICOM PN carets to spaces, blanks collapsed, upper case."""
    if not name:
        return None
    s = re.sub(r"\s+", " ", name.replace("^", " ")).strip().upper()
    return s or None


def _quantile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def _samples(session: Session, conds: list) -> list[tuple]:
    """(study id, device id, operator, exam code, age days, value)."""
    sys_id = session.scalar(select(CodeSystem.id).where(CodeSystem.key == NATIONAL))
    if sys_id is None:
        return []
    cols = (Study.id, Study.device_id, Study.operator_name, Code.code, Study.age_days)
    dose = func.coalesce(Study.total_dlp, Study.total_dap)
    out = list(session.execute(
        select(*cols, dose)
        .join(Classification, (Classification.study_id == Study.id)
              & Classification.event_id.is_(None))
        .join(Code, (Code.id == Classification.code_id) & (Code.system_id == sys_id))
        .where(*conds, Study.modality.in_(["CT", "RF", "XA"]), dose > 0)))
    out += session.execute(
        select(*cols, Event.dap)
        .join(Event, Event.study_id == Study.id)
        .join(Classification, Classification.event_id == Event.id)
        .join(Code, (Code.id == Classification.code_id) & (Code.system_id == sys_id))
        .where(*conds, Study.modality.in_(["DX", "CR"]), Event.dap > 0))
    return out


def build(session: Session, conds: list, start: dt.date, end: dt.date,
          min_n: int = 20) -> dict:
    band = BandLookup(session, "ro-drl-age", "age")
    names = {d.id: d.display_name or d.station_name or f"Device {d.id}"
             for d in session.scalars(select(Device))}
    meanings = {c: m for c, m in session.execute(
        select(Code.code, Code.meaning).join(CodeSystem).where(CodeSystem.key == NATIONAL))}
    groups: dict[tuple, list[float]] = defaultdict(list)
    rows = []
    for sid, did, op, code, age, v in _samples(session, conds):
        b = band(age)
        if b is None:
            continue
        groups[(code, b)].append(v)
        rows.append((sid, did, operator(op), code, b, v))
    ref = {g: (_quantile(vs, 0.5), _quantile(vs, 0.95))
           for g, vs in groups.items() if len(vs) >= MIN_GROUP}

    # coverage: per room, studies with and without an operator (whatever the comparison)
    cov: dict[int, dict] = defaultdict(lambda: {"studies": set(), "with": set()})
    per_op: dict[str, dict] = defaultdict(lambda: {"ratios": [], "above": 0, "studies": set(),
                                                   "devices": set(),
                                                   "exams": defaultdict(list)})
    for sid, did, op, code, b, v in rows:
        cov[did]["studies"].add(sid)
        if op:
            cov[did]["with"].add(sid)
        if not op or (code, b) not in ref:
            continue
        med, p95 = ref[(code, b)]
        o = per_op[op]
        o["ratios"].append(v / med)
        o["above"] += v > p95
        o["studies"].add(sid)
        o["devices"].add(did)
        o["exams"][(code, b)].append(v / med)

    operators = []
    for op, o in per_op.items():
        n = len(o["ratios"])
        ratio = _quantile(o["ratios"], 0.5)
        share = o["above"] / n
        exams = sorted(({"exam": c, "exam_meaning": meanings.get(c), "band": band.label.get(b, b),
                         "n": len(rs), "median_ratio": _quantile(rs, 0.5)}
                        for (c, b), rs in o["exams"].items()), key=lambda x: -x["n"])
        operators.append({
            "operator": op, "n": n, "studies": len(o["studies"]),
            "devices": sorted(names.get(d, str(d)) for d in o["devices"]),
            "median_ratio": ratio, "share_p95": share,
            "flag": n >= min_n and (ratio >= RATIO_FLAG or share >= P95_FLAG),
            "enough": n >= min_n, "exams": exams[:8]})
    operators.sort(key=lambda x: (not x["enough"], -x["median_ratio"]))
    coverage = sorted(({"device": names.get(d, str(d)), "studies": len(c["studies"]),
                        "with_operator": len(c["with"]),
                        "share": len(c["with"]) / len(c["studies"])}
                       for d, c in cov.items()), key=lambda x: -x["studies"])
    total = sum(c["studies"] for c in coverage)
    return {"start": start.isoformat(), "end": end.isoformat(), "min_n": min_n,
            "min_group": MIN_GROUP, "ratio_flag": RATIO_FLAG, "p95_flag": P95_FLAG,
            "groups_compared": len(ref), "operators": operators, "coverage": coverage,
            "studies": total,
            "studies_with_operator": sum(c["with_operator"] for c in coverage)}


def write_xlsx(rep: dict, target: IO, info: dict[str, object]) -> None:
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [30, 90])
        for k, v in [("Raport", "Doze pe operator, față de mediana departamentului pentru "
                                "aceeași examinare și grupă de vârstă"),
                     ("Perioada", f"{rep['start']} – {rep['end']}"), *info.items(),
                     ("Semnalare", f"≥ {rep['min_n']} valori și raport median ≥ "
                                   f"{rep['ratio_flag']:g} sau ≥ {rep['p95_flag']:.0%} peste P95"),
                     ("Scop", "Optimizare și instruire, nu sancționare."),
                     ("Notă", "CT: DLP pe studiu; radioscopie: DAP pe studiu; radiografie: "
                              "DAP pe incidență. Operatorul = Operators' Name (0008,1070).")]:
            about.row([k, v])
        ws = wb.sheet("Operatori", ["Operator", "Valori", "Studii", "Raport median",
                                    "Peste P95", "Semnalat", "Aparate"], [30, 8, 8, 12, 10, 9, 40])
        for o in rep["operators"]:
            ws.row([o["operator"], o["n"], o["studies"], round(o["median_ratio"], 3),
                    round(o["share_p95"], 3), "da" if o["flag"] else "",
                    ", ".join(o["devices"])])
        cv = wb.sheet("Acoperire", ["Aparat", "Studii", "Cu operator", "Pondere"], [30, 10, 12, 10])
        for c in rep["coverage"]:
            cv.row([c["device"], c["studies"], c["with_operator"], round(c["share"], 3)])
