"""Quarterly centralised report, Order MS 1255/2016 (INSP form), radiology and CT sheets.

For every national procedure code:
* the number of examinations in the quarter;
* the examinations by age band (0–12 months, 1–4, 5–9, 10–14, 15–39, ≥40 years) and sex;
* the mean dose per examination by age group (0–12 months, 1–4, 5–9, 10–14, ≥15 years):
  * radiography: DAP (Gy·cm²) of the projection, retakes included;
  * fluoroscopy: the study's DAP;
  * CT: the study's DLP (mGy·cm).

An "examination" is a study × procedure code: a chest study with an AP and a lateral
projection counts once under T1 and once under T2. ESAK/MGD are not collected, so those
columns stay empty.

The columns follow the official workbook in order, so the numeric block can be pasted into
it. Studies of external devices are left out by default (``origin:local``).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..db.models import Classification, Code, CodeSystem, Event, Patient, Study
from ..export.xlsx import XlsxWriter
from ..stats import BandLookup

COUNT_SCHEME, MEAN_SCHEME = "ro-1255-count-age", "ro-1255-mean-age"


@dataclass
class Sheet:
    key: str
    title: str
    system: str
    quantity: str
    unit: str
    rows: list[dict] = field(default_factory=list)


SHEETS = [
    Sheet("radiologie", "Radiologie", "ro-insp-1255-rx", "DAP mediu", "Gy·cm²"),
    Sheet("ct", "CT", "ro-insp-1255-ct", "DLP mediu", "mGy·cm"),
]


def quarter_range(year: int, quarter: int) -> tuple[dt.datetime, dt.datetime]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter is 1..4")
    start = dt.datetime(year, 3 * quarter - 2, 1)
    end = dt.datetime(year + 1, 1, 1) if quarter == 4 else dt.datetime(year, 3 * quarter + 1, 1)
    return start, end


def build(session: Session, conds: list, start: dt.datetime, end: dt.datetime) -> dict:
    counts_lk = BandLookup(session, COUNT_SCHEME, "age")
    means_lk = BandLookup(session, MEAN_SCHEME, "age")
    period = [Study.study_datetime >= start, Study.study_datetime < end, *conds]
    sheets = []
    notes = {"unknown_age": 0, "other_sex": 0}
    for spec in SHEETS:
        sheet = Sheet(spec.key, spec.title, spec.system, spec.quantity, spec.unit)
        codes = session.execute(select(Code.id, Code.code, Code.meaning).join(Code.system)
                                .where(CodeSystem.key == spec.system)
                                .order_by(Code.sort_order)).all()
        ids = [c.id for c in codes]
        units: dict[tuple[int, int], dict] = {}

        def unit(sid, cid, age, sex, units=units):
            return units.setdefault((sid, cid), {"age": age, "sex": sex, "values": []})

        if spec.key == "ct":
            q = (select(Classification.study_id, Classification.code_id, Study.age_days,
                        Patient.sex, Study.total_dlp)
                 .join(Study, Study.id == Classification.study_id)
                 .outerjoin(Patient, Patient.id == Study.patient_id)
                 .where(Classification.event_id.is_(None), Classification.code_id.in_(ids),
                        *period))
            for sid, cid, age, sex, v in session.execute(q):
                u = unit(sid, cid, age, sex)
                if v is not None:
                    u["values"].append(v)
        else:
            # radiography: projections on the exposures; fluoroscopy: the study
            q = (select(Classification.study_id, Classification.code_id, Study.age_days,
                        Patient.sex, Event.dap)
                 .join(Study, Study.id == Classification.study_id)
                 .join(Event, and_(Event.id == Classification.event_id))
                 .outerjoin(Patient, Patient.id == Study.patient_id)
                 .where(Classification.code_id.in_(ids), *period))
            for sid, cid, age, sex, v in session.execute(q):
                u = unit(sid, cid, age, sex)
                if v is not None:
                    u["values"].append(v)
            q = (select(Classification.study_id, Classification.code_id, Study.age_days,
                        Patient.sex, Study.total_dap)
                 .join(Study, Study.id == Classification.study_id)
                 .outerjoin(Patient, Patient.id == Study.patient_id)
                 .where(Classification.event_id.is_(None), Classification.code_id.in_(ids),
                        *period))
            for sid, cid, age, sex, v in session.execute(q):
                u = unit(sid, cid, age, sex)
                if v is not None and not u["values"]:
                    u["values"].append(v)

        per_code: dict[int, list[dict]] = defaultdict(list)
        for (_sid, cid), u in units.items():
            per_code[cid].append(u)
        for c in codes:
            exam, _, kind = c.meaning.partition(" — ")
            row = {"code": c.code, "exam": exam, "type": kind or None, "total": 0,
                   "counts": {b.key: {"M": 0, "F": 0} for b in counts_lk.bands},
                   "means": {b.key: None for b in means_lk.bands},
                   "n_dose": {b.key: 0 for b in means_lk.bands}}
            sums: dict[str, float] = defaultdict(float)
            for u in per_code.get(c.id, []):
                row["total"] += 1
                cb = counts_lk(u["age"])
                sex = (u["sex"] or "").upper()
                if cb is None:
                    notes["unknown_age"] += 1
                elif sex in ("M", "F"):
                    row["counts"][cb][sex] += 1
                else:
                    notes["other_sex"] += 1
                mb = means_lk(u["age"])
                if mb is not None and u["values"]:
                    sums[mb] += sum(u["values"])
                    row["n_dose"][mb] += 1
            for b, s in sums.items():
                row["means"][b] = s / row["n_dose"][b]
            sheet.rows.append(row)
        sheets.append(sheet)

    national = select(Classification.study_id).join(Code, Code.id == Classification.code_id) \
        .join(CodeSystem).where(CodeSystem.key.in_([s.system for s in SHEETS]))
    unclassified = session.scalar(
        select(func.count()).select_from(Study)
        .where(*period, Study.modality.in_(["DX", "CR", "RF", "CT"]),
               Study.id.not_in(national))) or 0
    return {"start": start.date().isoformat(), "end": (end - dt.timedelta(days=1)).date()
            .isoformat(), "sheets": sheets, "count_bands": counts_lk.bands,
            "mean_bands": means_lk.bands, "notes": {**notes, "unclassified": unclassified}}


def as_json(report: dict) -> dict:
    return {"period": {"from": report["start"], "to": report["end"]},
            "notes": report["notes"],
            "count_bands": [b.label for b in report["count_bands"]],
            "mean_bands": [b.label for b in report["mean_bands"]],
            "sheets": [{"key": s.key, "title": s.title, "quantity": s.quantity, "unit": s.unit,
                        "rows": s.rows} for s in report["sheets"]]}


def write_xlsx(report: dict, target: IO, info: dict[str, object]) -> None:
    cb, mb = report["count_bands"], report["mean_bands"]
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [34, 70])
        for k, v in [("Raport", "Raportare trimestrială centralizată (OMS 1255/2016)"),
                     ("Perioada", f"{report['start']} – {report['end']}"), *info.items(),
                     ("Examinări fără vârstă cunoscută", report["notes"]["unknown_age"]),
                     ("Examinări cu sex necunoscut / altul", report["notes"]["other_sex"]),
                     ("Studii fără cod național (neraportate)", report["notes"]["unclassified"]),
                     ("Notă", "O examinare = un studiu × un cod de procedură. Radiografie: "
                              "DAP al incidenței, inclusiv repetările. CT: DLP total al "
                              "studiului. ESAK/MGD nu sunt colectate.")]:
            about.row([k, v])
        for s in report["sheets"]:
            rx = s.key != "ct"
            head = (["Examinare", "Tip exp.", "Cod proc."] if rx else ["Examinare", "Cod proc."])
            head += ["Nr. total exam."]
            head += [f"{b.label} {sex}" for b in cb for sex in ("M", "F")]
            head += [f"{s.quantity} {b.label} ({s.unit})" for b in mb]
            if rx:
                head += [f"ESAK/MGD {b.label} (mGy)" for b in mb]
            ws = wb.sheet(s.title, head, [26, 10, 9, 9] + [7] * 12 + [12] * 10)
            for r in s.rows:
                cells = [r["exam"], r["type"], r["code"]] if rx else [r["exam"], r["code"]]
                cells.append(r["total"] or None)
                cells += [r["counts"][b.key][sex] or None for b in cb for sex in ("M", "F")]
                cells += [round(r["means"][b.key], 4 if rx else 1)
                          if r["means"][b.key] is not None else None for b in mb]
                if rx:
                    cells += [None] * len(mb)
                ws.row(cells)
