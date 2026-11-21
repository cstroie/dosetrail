"""Quarterly centralised report, Order MS 1255/2016 (INSP form).

Sheets:
* **Radiologie:** radiography, plus fluoroscopy (radioscopie rows); DAP and ESAK/MGD.
* **CT:** DLP.
* **Interventionala RX:** DAP and reference-point air kerma.
* **Interventionala CT:** DLP.
* **Dentare:** air kerma and DAP.

Nuclear medicine and radiotherapy are not collected by DoseTrail.

For every national procedure code:
* the number of examinations in the quarter;
* the examinations by age band and sex (radiology and CT: 0–12 months, 1–4, 5–9, 10–14,
  15–39, ≥40 years; interventional and dental: 0–4, 5–9, 10–14, 15–39, ≥40);
* the mean dose per examination by age group.

An "examination" is a study × procedure code: a chest study with an AP and a lateral
projection counts once under T1 and once under T2. Doses:
* radiography: the DAP of the projection (retakes included);
* fluoroscopy, interventional and dental: the study's DAP, and its Ka,r in mGy;
* CT: the study's DLP.

ESAK, MGD and the dental air kerma are not collected, so those columns stay empty.

The columns follow the official workbook in order, so each numeric block can be pasted
into it. Studies of external devices are left out by default (``origin:local``).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Classification, Code, CodeSystem, Event, Patient, Study
from ..export.xlsx import XlsxWriter
from ..stats import BandLookup


@dataclass
class Quantity:
    key: str | None  # dap | dlp | kar | None (not collected: empty column)
    label: str
    unit: str
    digits: int = 4


@dataclass
class Sheet:
    key: str
    title: str
    system: str
    count_scheme: str
    mean_scheme: str
    quantities: list[Quantity]
    has_type: bool = False  # the radiology sheet splits "Examinare" and "Tip exp."
    rows: list[dict] = field(default_factory=list)
    count_bands: list = field(default_factory=list)
    mean_bands: list = field(default_factory=list)


def sheets() -> list[Sheet]:
    dap = Quantity("dap", "DAP mediu", "Gy·cm²")
    return [
        Sheet("radiologie", "Radiologie", "ro-insp-1255-rx", "ro-1255-count-age",
              "ro-1255-mean-age", [dap, Quantity(None, "ESAK sau MGD", "mGy")], True),
        Sheet("ct", "CT", "ro-insp-1255-ct", "ro-1255-count-age", "ro-1255-mean-age",
              [Quantity("dlp", "DLP mediu", "mGy·cm", 1)]),
        Sheet("interventionala_rx", "Interventionala RX", "ro-insp-1255-xa",
              "ro-1255-int-count-age", "ro-1255-int-mean-age",
              [dap, Quantity("kar", "Doza medie în punctul de referință", "mGy", 1)]),
        Sheet("interventionala_ct", "Interventionala CT", "ro-insp-1255-ct-int",
              "ro-1255-int-count-age", "ro-1255-int-mean-age",
              [Quantity("dlp", "DLP mediu", "mGy·cm", 1)]),
        Sheet("dentare", "Dentare", "ro-insp-1255-dental", "ro-1255-int-count-age",
              "ro-1255-int-mean-age",
              [Quantity(None, "Kerma în aer", "mGy"), Quantity("dap", "DAP", "Gy·cm²")]),
    ]


SHEETS = sheets()  # for callers that only need the systems / titles


def quarter_range(year: int, quarter: int) -> tuple[dt.datetime, dt.datetime]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter is 1..4")
    start = dt.datetime(year, 3 * quarter - 2, 1)
    end = dt.datetime(year + 1, 1, 1) if quarter == 4 else dt.datetime(year, 3 * quarter + 1, 1)
    return start, end


def _units(session: Session, period: list, code_ids: list[int]) -> dict[tuple, dict]:
    """(study, code) -> {age, sex, dap, dlp, kar}: the doses of one examination."""
    units: dict[tuple, dict] = {}
    # exposure-level codes (radiography projections): sum of the exposures' DAP
    for sid, cid, age, sex, dap in session.execute(
            select(Classification.study_id, Classification.code_id, Study.age_days,
                   Patient.sex, Event.dap)
            .join(Study, Study.id == Classification.study_id)
            .join(Event, Event.id == Classification.event_id)
            .outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Classification.code_id.in_(code_ids), *period)):
        u = units.setdefault((sid, cid), {"age": age, "sex": sex, "dap": None, "dlp": None,
                                          "kar": None})
        if dap is not None:
            u["dap"] = (u["dap"] or 0.0) + dap
    # study-level codes: the study's totals
    for sid, cid, age, sex, dap, dlp, kar in session.execute(
            select(Classification.study_id, Classification.code_id, Study.age_days,
                   Patient.sex, Study.total_dap, Study.total_dlp, Study.total_rp_dose)
            .join(Study, Study.id == Classification.study_id)
            .outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Classification.event_id.is_(None), Classification.code_id.in_(code_ids),
                   *period)):
        if (sid, cid) in units:
            continue
        units[(sid, cid)] = {"age": age, "sex": sex, "dap": dap, "dlp": dlp,
                             "kar": kar * 1000 if kar is not None else None}  # Gy -> mGy
    return units


def build(session: Session, conds: list, start: dt.datetime, end: dt.datetime) -> dict:
    period = [Study.study_datetime >= start, Study.study_datetime < end, *conds]
    lookups: dict[str, BandLookup] = {}

    def lk(key):
        if key not in lookups:
            lookups[key] = BandLookup(session, key, "age")
        return lookups[key]

    out_sheets = []
    notes = {"unknown_age": 0, "other_sex": 0}
    for spec in sheets():
        codes = session.execute(select(Code.id, Code.code, Code.meaning).join(Code.system)
                                .where(CodeSystem.key == spec.system)
                                .order_by(Code.sort_order)).all()
        counts_lk, means_lk = lk(spec.count_scheme), lk(spec.mean_scheme)
        per_code: dict[int, list[dict]] = defaultdict(list)
        for (_sid, cid), u in _units(session, period, [c.id for c in codes]).items():
            per_code[cid].append(u)
        for c in codes:
            exam, _, kind = c.meaning.partition(" — ")
            row = {"code": c.code, "exam": exam if spec.has_type else c.meaning,
                   "type": (kind or None) if spec.has_type else None, "total": 0,
                   "counts": {b.key: {"M": 0, "F": 0} for b in counts_lk.bands},
                   "means": {q.key: {b.key: None for b in means_lk.bands}
                             for q in spec.quantities if q.key}}
            sums = {q.key: defaultdict(list) for q in spec.quantities if q.key}
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
                if mb is None:
                    continue
                for q, by_band in sums.items():
                    if u[q] is not None:
                        by_band[mb].append(u[q])
            for q, by_band in sums.items():
                for b, vals in by_band.items():
                    row["means"][q][b] = sum(vals) / len(vals)
            spec.rows.append(row)
        spec.count_bands, spec.mean_bands = counts_lk.bands, means_lk.bands
        out_sheets.append(spec)

    national = (select(Classification.study_id).join(Code, Code.id == Classification.code_id)
                .join(CodeSystem).where(CodeSystem.key.in_([s.system for s in out_sheets])))
    unclassified = session.scalar(
        select(func.count()).select_from(Study)
        .where(*period, Study.modality.in_(["DX", "CR", "RF", "XA", "CT"]),
               Study.id.not_in(national))) or 0
    return {"start": start.date().isoformat(),
            "end": (end - dt.timedelta(days=1)).date().isoformat(), "sheets": out_sheets,
            "notes": {**notes, "unclassified": unclassified,
                      "not_collected": "Medicină nucleară, radioterapie; ESAK/MGD; kerma "
                                       "în aer (dentar)"}}


def as_json(report: dict) -> dict:
    return {"period": {"from": report["start"], "to": report["end"]}, "notes": report["notes"],
            "sheets": [{"key": s.key, "title": s.title, "system": s.system,
                        "count_bands": [{"key": b.key, "label": b.label} for b in s.count_bands],
                        "mean_bands": [{"key": b.key, "label": b.label} for b in s.mean_bands],
                        "quantities": [{"key": q.key, "label": q.label, "unit": q.unit}
                                       for q in s.quantities],
                        "rows": s.rows} for s in report["sheets"]]}


def write_xlsx(report: dict, target: IO, info: dict[str, object]) -> None:
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [34, 70])
        n = report["notes"]
        for k, v in [("Raport", "Raportare trimestrială centralizată (OMS 1255/2016)"),
                     ("Perioada", f"{report['start']} – {report['end']}"), *info.items(),
                     ("Examinări fără vârstă cunoscută", n["unknown_age"]),
                     ("Examinări cu sex necunoscut / altul", n["other_sex"]),
                     ("Studii fără cod național (neraportate)", n["unclassified"]),
                     ("Necolectate de DoseTrail", n["not_collected"]),
                     ("Notă", "O examinare = un studiu × un cod de procedură. Radiografie: "
                              "DAP al incidenței, inclusiv repetările. Radioscopie, "
                              "intervențional, dentar: DAP și Ka,r ale studiului. CT: DLP "
                              "total al studiului.")]:
            about.row([k, v])
        for s in report["sheets"]:
            head = (["Examinare", "Tip exp.", "Cod proc."] if s.has_type
                    else ["Procedura", "Cod"]) + ["Nr. total exam."]
            head += [f"{b.label} {sex}" for b in s.count_bands for sex in ("M", "F")]
            for q in s.quantities:
                head += [f"{q.label} {b.label} ({q.unit})" for b in s.mean_bands]
            ws = wb.sheet(s.title, head, [30] + [10] * (len(head) - 1))
            for r in s.rows:
                cells = ([r["exam"], r["type"], r["code"]] if s.has_type
                         else [r["exam"], r["code"]]) + [r["total"] or None]
                cells += [r["counts"][b.key][sex] or None for b in s.count_bands
                          for sex in ("M", "F")]
                for q in s.quantities:
                    for b in s.mean_bands:
                        v = r["means"].get(q.key, {}).get(b.key) if q.key else None
                        cells.append(round(v, q.digits) if v is not None else None)
                ws.row(cells)
