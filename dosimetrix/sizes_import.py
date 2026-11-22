"""Patient weight / height and clinical indication from a CSV file (RIS export, ward
records, a spreadsheet).

Each row names one study, or a patient on a day, and gives a weight and/or a height:

* study key, the first one present wins:
  ``study_uid``; ``accession`` (also matches the hashed accessions of legacy imports);
  ``patient_id`` + ``date`` (every study of the patient that day, optionally only one
  ``modality``). Legacy patients whose ID was hashed match too.
* values: ``weight_kg``; ``height_m`` or ``height_cm`` (a height above 3 is read as cm);
  ``indication`` (the request's text: coded by indications.py, review on Admin →
  Indications) and/or ``indication_code`` (a ro-nom-indications code, kept as set);
* provenance, optional: ``source`` (``measured``, ``chart_same_day``, ``chart_72h``,
  ``imported``, ``his_chart_undated``, ``dicom_unknown``), or ``measured_at`` (date or
  date-time; same day as the exam → chart_same_day, within 72 h → chart_72h, else
  his_chart_undated). Neither → ``imported``.

Column names are case-insensitive and a few aliases are accepted (``weight``, ``greutate``,
``accession_number``, ``cnp``…). Delimiter ``,`` or ``;``; a decimal comma is accepted.

A value replaces the stored one only when its source ranks higher (measured > chart same
day > chart ≤ 72 h > imported / DICOM > undated chart > estimates) and only when it is
plausible for the patient's age and sex — the same rules as the HIS enrichment. After an
apply, the derived values (weight estimates, DRL exclusions, skin dose, alerts) are
recomputed for the changed studies.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from collections import Counter

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .db.models import Code, CodeSystem, Patient, SizeSource, Study, StudyIndication

ALIASES = {
    "study_uid": ("study_uid", "studyinstanceuid", "study_instance_uid", "suid"),
    "accession": ("accession", "accession_number", "accessionnumber", "acc", "nr_acces"),
    "patient_id": ("patient_id", "patientid", "pid", "cnp"),
    "date": ("date", "study_date", "exam_date", "data"),
    "modality": ("modality", "modalitate"),
    "weight_kg": ("weight_kg", "weight", "greutate", "greutate_kg", "kg"),
    "height_m": ("height_m", "height", "inaltime", "inaltime_m"),
    "height_cm": ("height_cm", "inaltime_cm", "cm"),
    "source": ("source", "weight_source", "sursa"),
    "measured_at": ("measured_at", "measured", "data_masurare"),
    "indication": ("indication", "indicatie", "indicație", "reason", "diagnostic"),
    "indication_code": ("indication_code", "cod_indicatie", "cod_indicație"),
}
SOURCES = {s.value: s for s in (SizeSource.measured, SizeSource.chart_same_day,
                                SizeSource.chart_72h, SizeSource.imported,
                                SizeSource.his_chart_undated, SizeSource.dicom_unknown)}


class RowError(ValueError):
    pass


def _norm(name: str) -> str:
    return name.strip().lstrip("﻿").lower().replace(" ", "_").replace("-", "_")


def _columns(header: list[str]) -> dict[str, int]:
    seen = {_norm(h): i for i, h in enumerate(header)}
    cols = {}
    for key, names in ALIASES.items():
        for n in names:
            if n in seen:
                cols[key] = seen[n]
                break
    return cols


def _num(v: str | None) -> float | None:
    v = (v or "").strip().replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise RowError(f"not a number: {v!r}") from None


def _when(v: str | None) -> dt.datetime | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S",
                "%d.%m.%Y %H:%M"):
        try:
            return dt.datetime.strptime(v, fmt)
        except ValueError:
            pass
    raise RowError(f"not a date: {v!r}")


def read(text: str) -> tuple[dict[str, int], list[list[str]]]:
    text = text.lstrip("﻿")
    first = text.split("\n", 1)[0]
    delim = ";" if first.count(";") > first.count(",") else ("\t" if "\t" in first else ",")
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        raise RowError("empty file")
    cols = _columns(rows[0])
    if not ({"study_uid", "accession", "patient_id"} & cols.keys()):
        raise RowError("no key column: study_uid, accession, or patient_id + date")
    if not ({"weight_kg", "height_m", "height_cm", "indication", "indication_code"}
            & cols.keys()):
        raise RowError("no value column: weight_kg, height_m, height_cm, indication or "
                       "indication_code")
    if "patient_id" in cols and "date" not in cols and not ({"study_uid", "accession"}
                                                            & cols.keys()):
        raise RowError("patient_id needs a date column")
    return cols, rows[1:]


def _sha(v: str) -> str:
    return hashlib.sha256(v.encode("utf-8")).hexdigest()


def _find(session: Session, get) -> list[Study]:
    if uid := get("study_uid"):
        return list(session.scalars(select(Study).where(Study.study_uid == uid)))
    if acc := get("accession"):
        return list(session.scalars(select(Study).where(or_(
            Study.accession == acc,
            (Study.accession == _sha(acc)) & Study.accession_is_hashed))))
    pid, day = get("patient_id"), get("date")
    if pid:
        d = _when(day)
        if d is None:
            raise RowError("patient_id without a date")
        start = dt.datetime.combine(d.date(), dt.time())
        q = (select(Study).join(Patient, Patient.id == Study.patient_id)
             .where(or_(Patient.patient_id == pid, Patient.legacy_sha256 == _sha(pid)),
                    Study.study_datetime >= start,
                    Study.study_datetime < start + dt.timedelta(days=1)))
        if mod := get("modality"):
            q = q.where(func.upper(Study.modality) == mod.upper())
        return list(session.scalars(q))
    raise RowError("no study key in this row")


def _source(get, study: Study) -> SizeSource:
    from .his.enrich import _size_source

    if s := get("source"):
        if s.lower() not in SOURCES:
            raise RowError(f"unknown source {s!r} (one of {', '.join(SOURCES)})")
        return SOURCES[s.lower()]
    if (at := _when(get("measured_at"))) is not None:
        return _size_source(at, study.study_datetime)
    return SizeSource.imported


def run(session: Session, text: str, *, apply: bool = False, max_issues: int = 200) -> dict:
    """Read the CSV, match and apply. Without ``apply`` nothing is written (dry run).

    Returns counts plus up to ``max_issues`` row issues ``{row, outcome, detail}``; row
    numbers count the header as row 1. Identifiers are not echoed back."""
    from .his.enrich import apply_sizes

    cols, rows = read(text)
    counts: Counter = Counter()
    issues: list[dict] = []
    changed: set[int] = set()
    nested = session.begin_nested()  # a dry run rolls back to here
    ind_codes = {c.code.upper(): c for c in session.scalars(
        select(Code).join(CodeSystem).where(CodeSystem.key == "ro-nom-indications"))}

    def note(rowno, outcome, detail=""):
        counts[outcome] += 1
        if len(issues) < max_issues:
            issues.append({"row": rowno, "outcome": outcome, "detail": detail})

    for rowno, row in enumerate(rows, start=2):
        if not any(c.strip() for c in row):
            continue
        counts["rows"] += 1

        def get(key, row=row):
            i = cols.get(key)
            return row[i].strip() if i is not None and i < len(row) and row[i].strip() \
                else None
        try:
            w = _num(get("weight_kg"))
            h = _num(get("height_m"))
            if h is None and (cm := _num(get("height_cm"))) is not None:
                h = cm / 100
            if h is not None and h > 3:
                h /= 100
            ind_text, ind_code = get("indication"), get("indication_code")
            code = None
            if ind_code:
                code = ind_codes.get(ind_code.upper())
                if code is None:
                    raise RowError(f"unknown indication code {ind_code!r}")
            if w is None and h is None and not ind_text and code is None:
                note(rowno, "no_value")
                continue
            studies = _find(session, get)
        except RowError as e:
            note(rowno, "error", str(e))
            continue
        if not studies:
            note(rowno, "not_found")
            continue
        counts["studies_matched"] += len(studies)
        for st in studies:
            if (ind_text and st.indication_source in (None, "dicom", "csv")
                    and st.indication_text != ind_text[:1024]):
                st.indication_text, st.indication_source = ind_text[:1024], "csv"
                counts["indication_written"] += 1
                changed.add(st.id)
            if code is not None:
                si = session.get(StudyIndication, st.id) or StudyIndication(study_id=st.id)
                si.code_id, si.status, si.score, si.source = code.id, "manual", 1.0, "csv"
                session.add(si)
                counts["indication_code_written"] += 1
            if w is None and h is None:
                continue
            try:
                src = _source(get, st)
            except RowError as e:
                note(rowno, "error", str(e))
                break
            sex = st.patient.sex if st.patient else None
            detail: dict = {}
            written = apply_sizes(st, sex, [("csv", w, h, src)], detail)
            for r in detail.get("rejected", []):
                note(rowno, "implausible", r["why"])
            if written:
                counts["studies_updated"] += 1
                changed.add(st.id)
                for what in written:
                    counts[what.split()[0] + "_written"] += 1
            elif not detail.get("rejected"):
                counts["kept_better_source"] += 1
    if not apply:
        nested.rollback()
    else:
        nested.commit()
        if changed:
            from .ingest.store import derive

            derive(session, sorted(changed))
    return {"applied": apply, "counts": dict(counts), "issues": issues,
            "columns": sorted(cols)}
