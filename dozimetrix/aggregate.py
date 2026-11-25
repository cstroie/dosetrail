"""Central collection for the national paediatric dose survey (aggregator mode).

The same Dozimetrix that collects locally can run centrally: institutions send their
anonymised monthly survey export (export/survey.py, CSV or JSON), the central team uploads
it here, and the national statistics come out of all uploads together.

**Upload.** The file is checked against the survey dictionary: version, required columns,
one institution and one month per file, numbers where numbers belong, plausible ranges.
Rejected rows are listed (row number and reason) and left out; the rest is stored. A newer
upload for the same institution and month replaces the older one (status ``replaced``).

**Statistics** (the draft plan; ICRP 135):
* groups: head exams by the RP 185 head age group, body exams by the body weight group
  (the grid's head/body rule, grid.py), or the national age groups (OMS 838/2025);
* rows with a DRL exclusion (weight < P5 / > P95 for age, prematurity) are left out; so are
  estimated weights from the weight groups unless asked for, and head CT series reported on
  the 32 cm phantom (counted per row in ``n_phantom32``);
* per institution: n and median (counted with at least ``min_n`` values: 10, fluoroscopy
  20);
* national: the **P75 of the institution medians** (the proposed NDRL), the national
  median of all values, and the interquartile factor Q3/Q1 of all values (flags at 1.5,
  2.5 and 3: a wide spread asks for protocol harmonisation);
* each against the RP 185 EDRL (and the national DRL in the national grid): below, up to
  1.5×, above 1.5×.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from typing import IO

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .db.models import SurveyInstitution, SurveyRow, SurveyUpload
from .stats import describe

NUMERIC = ("age_days", "age_months", "age_years", "weight_kg", "weight_reported_kg",
           "weight_percentile", "height_m", "series", "ctdivol", "phantom", "dlp",
           "scan_length", "kvp", "mas", "effective_mas", "pitch", "ssde", "pka", "fluoro_time",
           "kae", "exposures")
REQUIRED = ("survey_version", "institution", "modality", "period")
FREE_TEXT = ("indication_text",)  # may name people or places: not kept centrally
TYPED = ("device", "manufacturer", "model", "modality", "patient", "sex", "head_age_group",
         "weight_kg", "weight_source", "weight_group", "drl_exclusion", "indication",
         "exam_code", "edrl_exam", "ndrl_exam", "projection", "contrast", "ctdivol",
         "phantom", "dlp", "pka", "fluoro_time", "ssde", "check_dlp", "check_phantom")
LIMITS = {"weight_kg": (0.3, 250), "ctdivol": (0, 500), "dlp": (0, 20000),
          "pka": (0, 5e6), "fluoro_time": (0, 36000), "age_years": (0, 120),
          "phantom": (16, 32)}
MIN_N = {"CT": 10, "DX": 10, "CR": 10, "RX": 10, "MG": 10, "RF": 20, "XA": 20}
HEAD_ALIAS = {"6y+": "6-18y"}  # export (RP 185) key -> the grid's extended key
STATUS = ("below", "above", "well_above")


class SurveyError(ValueError):
    pass


# ------------------------------------------------------------------------------ upload


def _read(content: bytes, filename: str | None) -> tuple[list[dict], dict]:
    text = content.decode("utf-8-sig", errors="strict")
    if (filename or "").lower().endswith(".json") or text.lstrip().startswith("{"):
        doc = json.loads(text)
        if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list) \
                or not all(isinstance(r, dict) for r in doc["rows"]):
            raise KeyError("expected an object with a list of row objects under 'rows'")
        meta = {k: v for k, v in doc.items() if k not in ("rows", "dictionary")}
        return [{k: ("" if v is None else v) for k, v in r.items()} for r in doc["rows"]], meta
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows, {}


def _clean(v):
    """CSV cells written spreadsheet-safe ("'-5") lose the apostrophe."""
    if isinstance(v, str):
        v = v.strip()
        if v[:2] in ("'=", "'+", "'-", "'@"):
            v = v[1:]
    return v


def _num(v):
    if v in ("", None):
        return None
    x = float(str(v).replace(",", "."))
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"{v!r} is not a finite number")
    return x


def _age_years(r: dict) -> float | None:
    for key, div in (("age_days", 365.25), ("age_months", 12.0), ("age_years", 1.0)):
        if r.get(key) is not None:
            return r[key] / div
    return None


def upload(session: Session, content: bytes, *, filename: str | None = None,
           user: str | None = None, max_issues: int = 200) -> SurveyUpload:
    """Validate and store one export. Raises SurveyError when the whole file is unusable."""
    from .export.survey import FIELDS, SURVEY_VERSION

    try:
        raw, meta = _read(content, filename)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as e:
        raise SurveyError(f"not a survey export (CSV or JSON): {e}") from e
    if not raw:
        raise SurveyError("the file has no rows")
    missing = [c for c in REQUIRED if c not in raw[0]]
    if missing:
        raise SurveyError(f"missing column(s): {', '.join(missing)}")
    unknown = sorted(set(raw[0]) - set(FIELDS))
    versions = {_clean(r.get("survey_version")) for r in raw}
    insts = {_clean(r.get("institution")) for r in raw}
    periods = {_clean(r.get("period")) for r in raw}
    if len(insts) != 1 or len(periods) != 1:
        raise SurveyError("one institution and one month per file "
                          f"(found {len(insts)} institutions, {len(periods)} months)")
    inst, period = insts.pop(), periods.pop()
    try:
        dt.date.fromisoformat(f"{period}-01")
    except ValueError:
        raise SurveyError(f"period {period!r} is not YYYY-MM") from None
    issues, rows, rejected = [], [], Counter()
    for i, r in enumerate(raw, start=2):
        r = {k: _clean(v) for k, v in r.items()}
        try:
            for k in NUMERIC:
                if k in r:
                    r[k] = _num(r[k])
            r["age_years_total"] = _age_years(r)
            for k, (lo, hi) in LIMITS.items():
                v = r.get(k) if k != "age_years" else r["age_years_total"]
                if v is not None and not lo <= v <= hi:
                    raise SurveyError(f"{k} {v:g} outside {lo:g}–{hi:g}")
            if (r.get("modality") or "").upper() == "CT" and r.get("dlp") is None \
                    and r.get("ctdivol") is None:
                raise SurveyError("CT row without DLP or CTDIvol")
            if (r.get("modality") or "").upper() != "CT" and r.get("pka") is None:
                raise SurveyError("row without PKA (DAP)")
        except (SurveyError, ValueError) as e:
            rejected[re.sub(r"-?\d[\d.]*", "#", str(e))] += 1
            if len(issues) < max_issues:
                issues.append({"row": i, "reason": str(e)})
            continue
        rows.append(r)
    digest = hashlib.sha256(content).hexdigest()
    old = list(session.scalars(select(SurveyUpload).filter_by(
        institution=inst, period=period, status="active")))
    if any(o.sha256 == digest for o in old):
        raise SurveyError("this file was uploaded already")
    for o in old:
        o.status = "replaced"
        session.execute(delete(SurveyRow).where(SurveyRow.upload_id == o.id))
    up = SurveyUpload(institution=inst, period=period, survey_version=",".join(sorted(
        v for v in versions if v)), filename=(filename or "")[:255] or None, sha256=digest,
        uploaded_by=user, status="active", n_rows=len(rows), n_rejected=len(raw) - len(rows),
        report={"issues": issues, "rejected_by": dict(rejected), "unknown_columns": unknown,
                "version_expected": SURVEY_VERSION, "replaced": [o.id for o in old],
                "meta": {k: meta[k] for k in list(meta)[:20]}})
    session.add(up)
    session.flush()
    if session.get(SurveyInstitution, inst) is None:
        session.add(SurveyInstitution(code=inst))
    known = set(TYPED)
    session.bulk_insert_mappings(SurveyRow, [{
        "upload_id": up.id, "institution": inst, "period": period,
        "age_years": r["age_years_total"], **_typed(r),
        "extra": {k: v for k, v in r.items() if k not in known and k not in REQUIRED
                  and k not in FREE_TEXT and k != "age_years_total" and v not in ("", None)},
    } for r in rows])
    session.flush()
    return up


def _typed(r: dict) -> dict:
    out = {}
    for k in TYPED:
        v = r.get(k)
        if v in ("", None):
            out[k] = None
        elif k == "phantom":
            out[k] = int(v)
        elif k == "modality":
            out[k] = str(v).upper()
        else:
            out[k] = v
    return out


def uploads(session: Session) -> list[dict]:
    names = {i.code: i.name for i in session.scalars(select(SurveyInstitution))}
    return [{"id": u.id, "institution": u.institution, "institution_name": names.get(u.institution),
             "period": u.period, "version": u.survey_version, "filename": u.filename,
             "uploaded_at": u.uploaded_at.isoformat(timespec="seconds") if u.uploaded_at
             else None, "uploaded_by": u.uploaded_by, "status": u.status, "rows": u.n_rows,
             "rejected": u.n_rejected, "report": u.report}
            for u in session.scalars(select(SurveyUpload).order_by(
                SurveyUpload.period.desc(), SurveyUpload.institution, SurveyUpload.id.desc()))]


def remove(session: Session, upload_id: int) -> dict:
    up = session.get(SurveyUpload, upload_id)
    if up is None:
        raise SurveyError("upload not found")
    out = {"id": up.id, "institution": up.institution, "period": up.period, "rows": up.n_rows}
    session.execute(delete(SurveyRow).where(SurveyRow.upload_id == up.id))
    session.delete(up)
    session.flush()
    return out


def name_institution(session: Session, code: str, name: str | None) -> None:
    inst = session.get(SurveyInstitution, code)
    if inst is None:
        raise SurveyError(f"unknown institution {code!r}")
    inst.name = (name or "").strip()[:255] or None
    session.flush()


# -------------------------------------------------------------------------- statistics


def _iq_flag(f: float | None) -> str | None:
    if f is None:
        return None
    return "ok" if f <= 1.5 else "wide" if f <= 2.5 else "very_wide" if f <= 3 else "extreme"


def _status(v: float | None, ref: float | None) -> str | None:
    if v is None or not ref:
        return None
    r = v / ref
    return "below" if r <= 1 else "above" if r <= 1.5 else "well_above"


def national(session: Session, start: str, end: str, *, grid: str = "rp185",
             min_n: dict | None = None, estimated_weights: bool = False) -> dict:
    """National statistics over the active uploads with period in [start, end] (YYYY-MM)."""
    from .grid import Grid
    from .localdrl import NATIONAL, NATIONAL_SCHEME, _references
    from .reports.grid import _edrl
    from .stats import BandLookup

    if grid not in ("rp185", "national"):
        raise SurveyError("grid is rp185 or national")
    need = {**MIN_N, **(min_n or {})}
    g = Grid(session)
    age_lk = BandLookup(session, NATIONAL_SCHEME, "age") if grid == "national" else None
    edrl = _edrl(session)  # (national exam, ext band, quantity) -> local unit
    ndrl = _references(session, "national") if grid == "national" else {}
    names = {i.code: i.name for i in session.scalars(select(SurveyInstitution))}
    q = (select(SurveyRow).join(SurveyUpload, SurveyUpload.id == SurveyRow.upload_id)
         .where(SurveyUpload.status == "active", SurveyRow.period >= start,
                SurveyRow.period <= end))
    values: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    counts: Counter = Counter()
    phantom32: Counter = Counter()  # head CT values measured on the 32 cm phantom
    insts_seen: set[str] = set()
    for r in session.scalars(q):
        counts["rows"] += 1
        insts_seen.add(r.institution)
        if r.drl_exclusion:
            counts["excluded"] += 1
            continue
        exam = r.ndrl_exam
        if not exam:
            counts["no_exam_code"] += 1
            continue
        if grid == "national":
            band = age_lk(int(r.age_years * 365.25)) if r.age_years is not None else None
        elif g.parameter(NATIONAL, exam) == "age":
            band = HEAD_ALIAS.get(r.head_age_group, r.head_age_group)
        else:
            est = (r.weight_source or "").startswith("estimated")
            band = r.weight_group if (estimated_weights or not est) else None
        if not band:
            counts["no_group"] += 1
            continue
        mod = (r.modality or "").upper()
        fam = "CT" if mod == "CT" else mod
        quants = [("dlp", r.dlp, "mGy.cm"), ("ctdivol", r.ctdivol, "mGy")] if mod == "CT" \
            else [("dap", r.pka / 1000 if r.pka is not None else None, "Gy.cm2")]
        head32 = g.parameter(NATIONAL, exam) == "age" and r.phantom == 32
        for quantity, v, unit in quants:
            if v is None:
                continue
            if head32:  # about half the 16 cm value: left out, counted
                phantom32[(exam, band, quantity, unit, fam)] += 1
                continue
            values[(exam, band, quantity, unit, fam)][r.institution].append(v)
    from .db.models import Code, CodeSystem

    labels = dict(session.execute(select(Code.code, Code.meaning).join(CodeSystem)
                                  .where(CodeSystem.key == NATIONAL)).all())
    rows = []
    for (exam, band, quantity, unit, fam), per_inst in sorted(values.items()):
        n_min = need.get(fam, 10)
        insts, medians, pooled = [], [], []
        for inst, vals in sorted(per_inst.items()):
            d = describe(vals)
            ok = d["n"] >= n_min
            insts.append({"institution": inst, "name": names.get(inst), "n": d["n"],
                          "median": d["median"], "counts": ok})
            pooled += vals
            if ok:
                medians.append(d["median"])
        p = describe(pooled)
        ndrl_prop = describe(medians)["p75"] if len(medians) >= 2 else None
        iq = p["p75"] / p["p25"] if p["n"] >= 4 and p["p25"] else None
        eu = edrl.get((exam, band, quantity))
        nat = ndrl.get((exam, band, quantity))
        rows.append({
            "exam": exam, "exam_meaning": labels.get(exam, exam), "group": band,
            "quantity": quantity, "unit": unit, "n": p["n"], "n_institutions": len(per_inst),
            "n_institutions_counted": len(medians), "min_n": n_min,
            "n_phantom32": phantom32.get((exam, band, quantity, unit, fam), 0),
            "national_median": p["median"], "ndrl_proposal": ndrl_prop,
            "iq_factor": round(iq, 2) if iq else None, "iq_flag": _iq_flag(iq),
            "edrl": eu, "status_edrl": _status(ndrl_prop, eu),
            "ndrl": nat, "status_ndrl": _status(ndrl_prop, nat),
            "institutions": insts})
    return {"start": start, "end": end, "grid": grid, "estimated_weights": estimated_weights,
            "institutions": sorted(insts_seen), "counts": dict(counts), "rows": rows}


def write_xlsx(rep: dict, target: IO, info: dict[str, object]) -> None:
    from .export.xlsx import XlsxWriter

    st = {"below": "≤ referință", "above": "≤ 1,5 × referință", "well_above": "> 1,5 × referință",
          None: ""}
    iq = {"ok": "≤ 1,5", "wide": "1,5–2,5", "very_wide": "2,5–3", "extreme": "> 3", None: ""}
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [30, 90])
        for k, v in [("Raport", "Studiul național de dozimetrie pediatrică — statistici "
                                "centrale"), ("Perioada", f"{rep['start']} – {rep['end']}"),
                     ("Grupe", "RP 185 (cap: vârstă, corp: greutate)" if rep["grid"] == "rp185"
                      else "grupele naționale de vârstă (OMS 838/2025)"),
                     ("Instituții", len(rep["institutions"])),
                     *[(f"Rânduri {k}", v) for k, v in rep["counts"].items()],
                     ("Metodă", "NRD propus = P75 al medianelor instituționale (instituții cu "
                                "n ≥ minim: 10, radioscopie 20); mediana națională și factorul "
                                "Q3/Q1 pe toate valorile"), *info.items()]:
            about.row([k, v])
        ws = wb.sheet("Statistici", ["Examinare", "Cod", "Grupa", "Mărime", "Unitate", "n",
                                     "Instituții (cu n minim)", "Mediana națională",
                                     "NRD propus (P75)", "Q3/Q1", "Dispersie", "EDRL",
                                     "vs EDRL", "NRD OMS 838", "vs NRD"],
                      [30, 16, 10, 8, 8, 7, 12, 11, 11, 7, 10, 9, 14, 10, 14])
        for r in rep["rows"]:
            ws.row([r["exam_meaning"], r["exam"], r["group"], r["quantity"], r["unit"], r["n"],
                    f"{r['n_institutions']} ({r['n_institutions_counted']})",
                    r["national_median"], r["ndrl_proposal"], r["iq_factor"], iq[r["iq_flag"]],
                    r["edrl"], st[r["status_edrl"]], r["ndrl"], st[r["status_ndrl"]]])
        inst = wb.sheet("Institutii", ["Examinare", "Grupa", "Mărime", "Instituție", "Nume",
                                       "n", "Mediana", "Contează"], [16, 10, 8, 14, 24, 7, 10, 9])
        for r in rep["rows"]:
            for i in r["institutions"]:
                inst.row([r["exam"], r["group"], r["quantity"], i["institution"], i["name"],
                          i["n"], i["median"], "da" if i["counts"] else "nu"])
