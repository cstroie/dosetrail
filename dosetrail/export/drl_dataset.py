"""DoseTrail DRL dataset (versioned) and the ICRP 135 summary.

**Dataset**: one row per study × DRL exam, the core for local DRL work and national surveys.
* The period is reduced to the month (YYYY-MM).
* Patients, devices and the facility are keyed pseudonymous codes: HMAC with the site
  secret. Repeat examinations can be counted, but codes can't be reversed without the
  secret.
* Each dose has a fixed UCUM unit. A data dictionary (``DICTIONARY``) ships with every
  file.
* Radiography rows group the exposures of one projection in a study (retakes included);
  CT rows cover the study's non-localiser series; fluoroscopy rows cover the whole study.

**ICRP 135 summary**: per room and for the facility (pooled), for each exam × patient group
× quantity of a reference set: n, median, mean, SD, P25/P75 and min/max; a flag when n is
below the minimum sample size; and the median compared with the reference value.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from collections import defaultdict
from collections.abc import Iterator
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__ as VERSION
from ..db.models import Classification, Code, CodeSystem, Device, Event, Patient, Study
from ..patient_age import Band, band_for_age
from ..stats import load_bands
from .xlsx import XlsxWriter

DATASET_VERSION = "1.0"
NATIONAL, EUROPEAN = "ro-drl-ms838-2025", "ec-rp185"
NOMENCLATORS = ("ro-nom-ct", "ro-nom-rx")
EXPOSURE_EXAM = ("DX", "CR", "MG", "PX")
BATCH = 2000

# name, unit, description
DICTIONARY: list[tuple[str, str, str]] = [
    ("dataset_version", "", "Version of this dataset layout"),
    ("facility", "", "Facility code (keyed pseudonym)"),
    ("device", "", "Device code (keyed pseudonym of the physical device)"),
    ("modality", "", "DICOM modality"),
    ("manufacturer", "", "Device manufacturer"),
    ("model", "", "Device model"),
    ("period", "", "Month of the examination, YYYY-MM"),
    ("patient", "", "Patient code (keyed pseudonym; equal codes = same patient)"),
    ("sex", "", "M / F / O"),
    ("age_days", "d", "Age in completed days, below 1 year only"),
    ("age_months", "mo", "Age in completed months, below 24 months only"),
    ("age_years", "a", "Age in completed years"),
    ("age_group", "", "National DRL age group (OMS 838/2025): 1-4y, 5-9y, 10-14y, 15y+"),
    ("weight_kg", "kg", "Patient weight"),
    ("weight_source", "", "measured / chart_same_day / chart_72h / estimated_* / dicom_unknown"),
    ("height_m", "m", "Patient height"),
    ("local_exam", "", "Local study description"),
    ("exam_code", "", "Local nomenclator code (ro-nom-ct / ro-nom-rx)"),
    ("drl_exam_national", "", "Exam code of the national DRL list (OMS 838/2025)"),
    ("drl_exam_european", "", "Exam code of the European paediatric DRLs (RP 185)"),
    ("projection", "", "Projection for radiography: AP, PA, LL, OBL…"),
    ("contrast", "", "CT contrast: N, C, NC, MF"),
    ("events", "1", "Irradiation events in this row (series, exposures)"),
    ("dlp", "mGy.cm", "Sum of DLP of the CT series (localisers excluded)"),
    ("ctdivol_mean", "mGy", "Mean CTDIvol of the CT series"),
    ("ctdivol_max", "mGy", "Highest CTDIvol of the CT series"),
    ("phantom", "cm", "CTDI phantom of the series: 16 or 32"),
    ("dap", "Gy.cm2", "Sum of dose-area product"),
    ("kar", "Gy", "Sum of air kerma at the patient entrance reference point"),
    ("fluoro_time", "s", "Fluoroscopy time"),
    ("kvp_mean", "kV", "Mean tube voltage"),
    ("mas_total", "mA.s", "Sum of tube current-time product"),
]
FIELDS = [d[0] for d in DICTIONARY]


def _code(secret: bytes, kind: str, value) -> str:
    mac = hmac.new(secret, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"{kind[0].upper()}-{mac[:8].upper()}"


def coarse_age(days: int | None) -> dict:
    """Age as the survey plan asks: days below 1 year, months up to 24 months, completed
    years from 2 years on. Finer values are left out (with month, sex and device they would
    narrow down the birth date)."""
    if days is None:
        return {"age_days": None, "age_months": None, "age_years": None}
    months = int(days / 30.4375)
    return {"age_days": days if days < 365 else None,
            "age_months": months if months < 24 else None,
            "age_years": int(days / 365.25)}


def patient_code(secret: bytes, p) -> str | None:
    """Stable across re-imports: keyed on the patient hash, not the database id."""
    if p is None:
        return None
    return _code(secret, "patient", p.legacy_sha256 or f"id:{p.id}")


def device_code(secret: bytes, d) -> str | None:
    return _code(secret, "device", d.identity_key or f"id:{d.id}") if d is not None else None


def _r(x, nd=6):
    return round(x, nd) if isinstance(x, float) else x


def dataset_rows(session: Session, conds: list, secret: bytes,
                 facility: str = "DEFAULT") -> Iterator[dict]:
    from ..classify import view_code

    _, nat_bands = load_bands(session, "ro-drl-age")
    ids = list(session.scalars(select(Study.id).where(*conds)
                               .order_by(Study.study_datetime, Study.id)))
    devices = {d.id: d for d in session.scalars(select(Device))}
    physical = {i: (d.merged_into_id or i) for i, d in devices.items()}
    fac = _code(secret, "facility", facility)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = session.execute(
            select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.id.in_(chunk))).all()
        cls = session.execute(
            select(Classification.study_id, Classification.event_id, CodeSystem.key, Code.code)
            .join(Code, Code.id == Classification.code_id).join(CodeSystem)
            .where(Classification.study_id.in_(chunk)))
        study_codes: dict[int, dict[str, str]] = defaultdict(dict)
        event_codes: dict[int, dict[str, str]] = defaultdict(dict)
        for sid, eid, system, code in cls:
            (study_codes[sid] if eid is None else event_codes[eid]).setdefault(system, code)
        events: dict[int, list[Event]] = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_(chunk),
                                                     Event.is_localizer.is_not(True))):
            events[e.study_id].append(e)
        for s, p in sorted(studies, key=lambda x: (x[0].study_datetime or dt.datetime.min,
                                                   x[0].id)):
            sc = study_codes.get(s.id, {})
            groups: dict[tuple, list[Event]] = defaultdict(list)
            for e in events.get(s.id, []):
                if s.modality in EXPOSURE_EXAM:
                    ec = event_codes.get(e.id, {})
                    key = (ec.get(NATIONAL), ec.get(EUROPEAN), ec.get("ro-nom-rx"),
                           view_code(e.view, e.protocol))
                else:  # a CT series of another region than its study has its own codes
                    c = event_codes.get(e.id) or sc
                    key = (c.get(NATIONAL), c.get(EUROPEAN),
                           next((c[k] for k in NOMENCLATORS if k in c), None), None)
                if key[0] or key[1]:
                    groups[key].append(e)
            for (nat, eu, nom, view), evs in groups.items():
                yield _row(s, p, devices, physical, secret, fac, nat_bands, nat, eu, nom,
                           view, evs)
        session.expunge_all()


def _row(s, p, devices, physical, secret, fac, nat_bands: list[Band], nat, eu, nom, view, evs):
    d = devices.get(physical.get(s.device_id)) if s.device_id else None
    band = band_for_age(nat_bands, days=s.age_days) if s.age_days is not None else None

    def total(attr):
        v = [getattr(e, attr) for e in evs if getattr(e, attr) is not None]
        return _r(sum(v)) if v else None

    ctdi = [e.ctdivol for e in evs if e.ctdivol is not None]
    kvp = [e.kvp for e in evs if e.kvp is not None]
    phantoms = {e.ct_phantom_cm for e in evs if e.ct_phantom_cm}
    return {
        "dataset_version": DATASET_VERSION, "facility": fac,
        "device": device_code(secret, d),
        "modality": s.modality, "manufacturer": d.manufacturer if d else None,
        "model": d.model_name if d else None,
        "period": s.study_datetime.strftime("%Y-%m") if s.study_datetime else None,
        "patient": patient_code(secret, p),
        "sex": p.sex if p else None, **coarse_age(s.age_days),
        "age_group": band.key if band else None,
        "weight_kg": s.weight_kg,
        "weight_source": s.weight_source.value if s.weight_source else None,
        "height_m": s.height_m, "local_exam": s.study_description, "exam_code": nom,
        "drl_exam_national": nat, "drl_exam_european": eu, "projection": view,
        "contrast": s.contrast_mode, "events": len(evs),
        "dlp": total("dlp"), "ctdivol_mean": _r(sum(ctdi) / len(ctdi), 3) if ctdi else None,
        "ctdivol_max": _r(max(ctdi), 3) if ctdi else None,
        "phantom": phantoms.pop() if len(phantoms) == 1 else None,
        "dap": total("dap"), "kar": total("rp_dose"), "fluoro_time": total("fluoro_time_s"),
        "kvp_mean": _r(sum(kvp) / len(kvp), 1) if kvp else None, "mas_total": total("mas"),
    }


def metadata(q: str, now: dt.datetime, rows: int | None = None) -> dict:
    return {"dataset": "dosetrail-drl", "version": DATASET_VERSION,
            "software": f"DoseTrail {VERSION}", "generated": now.isoformat(timespec="seconds"),
            "query": q or "(all)",
            "method": "ICRP 135 / RP 185; CT per study without localisers; radiography per "
                      "projection with retakes; units UCUM; period by month; keyed pseudonyms",
            **({"rows": rows} if rows is not None else {})}


def write(fmt: str, target: IO, data: Iterator[dict], meta: dict,
          dictionary: list[tuple[str, str, str]] | None = None) -> int:
    """Write rows as json / csv / xlsx with the data dictionary and metadata."""
    dictionary = dictionary or DICTIONARY
    fields = [d[0] for d in dictionary]
    n = 0
    if fmt == "json":
        out = {**meta, "dictionary": [{"name": a, "unit": b, "description": c}
                                      for a, b, c in dictionary], "rows": []}
        for r in data:
            out["rows"].append(r)
            n += 1
        out["rows_total"] = n
        target.write(json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"))
        return n
    if fmt == "csv":
        import csv
        import io

        text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="")
        w = csv.DictWriter(text, fields, lineterminator="\r\n")
        w.writeheader()
        from .lists import safe_text

        for r in data:
            w.writerow({k: ("" if v is None else safe_text(v)) for k, v in r.items()})
            n += 1
        text.flush()
        text.detach()
        return n
    with XlsxWriter(target) as wb:
        ws = wb.sheet("Data", fields, [max(8, min(len(f) + 2, 22)) for f in fields])
        for r in data:
            ws.row([r[f] for f in fields])
            n += 1
        dic = wb.sheet("Dictionary", ["Field", "Unit", "Description"], [22, 10, 80])
        for row in dictionary:
            dic.row(list(row))
        about = wb.sheet("About", ["Item", "Value"], [16, 90])
        for k, v in {**meta, "rows": n}.items():
            about.row([k, v])
    return n


# ------------------------------------------------------------------ ICRP 135 summary

SUMMARY_FIELDS = ["unit", "device", "exam", "exam_name", "group", "quantity", "applies_to",
                  "n", "median", "mean", "sd", "p25", "p75", "min", "max", "drl", "drl_unit",
                  "median_over_drl", "status", "sample_ok"]


def summary_rows(session: Session, conds: list, set_key: str, min_n: int = 20) -> list[dict]:
    from ..drl import compare

    out = []
    for by_device in (True, False):
        res = compare(session, conds, set_key, by_device=by_device, min_n=min_n)
        for r in res["rows"]:
            for g in r["groups"]:
                out.append({
                    "unit": "device" if by_device else "facility",
                    "device": g.get("device_name") if by_device else "(all rooms)",
                    "exam": r["exam"], "exam_name": r["exam_meaning"],
                    "group": r["band_label"] or "all", "quantity": r["quantity"],
                    "applies_to": r["applies_to"], "n": g["n"],
                    **{k: _r(g.get(k)) for k in ("median", "mean", "sd", "p25", "p75", "min",
                                                 "max")},
                    "drl": r["reference"], "drl_unit": r["unit"],
                    "median_over_drl": _r(g.get("ratio"), 3), "status": g["status"],
                    "sample_ok": g["n"] >= min_n})
    return out
