"""Anonymised export for the national paediatric dose survey (draft plan
MS-CNCAN-SDNP-2026-001, v1.0, March 2026). The layout is versioned so the plan can change.

* **Rows:** CT, one row per scan series (localisers excluded); radiography, one row per
  projection of a study (retakes summed); fluoroscopy, one row per study.
* **Patients:** paediatric only by default (age < 18 years).
* **Groups:** the RP 185 head age group and the body weight group (with 80–110 kg) are
  both given, so the analysis can pick by exam.
* **Anonymisation:** no name, national ID or birth date; the month of the exam only; age
  in days below 1 year, months up to 24 months, completed years after; keyed pseudonymous
  codes for patient, device and institution (HMAC with the site secret, keyed on stable
  identities so codes survive a re-import), so repeats can be counted centrally.
* **Validation flags** (from the plan):
  * ``check_dlp``: CTDIvol × scan length vs DLP within ±10 %;
  * ``check_phantom``: a head exam measured with the 32 cm phantom;
  * ``weight_missing``.
* **Not collected yet:** clinical indication, reconstruction method, Ka,e and WHO
  weight-for-age percentiles. Their columns are present and empty, and the dictionary says
  so.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Classification, Code, CodeSystem, Device, Event, Patient, Study
from ..patient_age import band_for_age, band_for_weight
from ..stats import load_bands
from .drl_dataset import (
    EUROPEAN,
    NATIONAL,
    _code,
    _r,
    coarse_age,
    device_code,
    patient_code,
)

SURVEY_VERSION = "2026.1"
EXPOSURE_EXAM = ("DX", "CR", "MG", "PX")
HEAD_CODES = {"CT-HEAD", "RX-HEAD-AP-PA", "RX-HEAD-LAT"}
BATCH = 2000

DICTIONARY: list[tuple[str, str, str]] = [
    ("survey_version", "", "Layout version of this export"),
    ("institution", "", "Institution code (keyed pseudonym)"),
    ("device", "", "Device code (keyed pseudonym)"),
    ("manufacturer", "", "Device manufacturer"),
    ("model", "", "Device model"),
    ("modality", "", "CT / DX / CR / RF / XA"),
    ("period", "", "Month of the examination, YYYY-MM"),
    ("patient", "", "Patient code (keyed pseudonym; equal codes = same patient)"),
    ("sex", "", "M / F / O"),
    ("age_days", "d", "Age in completed days, below 1 year only"),
    ("age_months", "mo", "Age in completed months, below 24 months only"),
    ("age_years", "a", "Age in completed years"),
    ("head_age_group", "", "RP 185 head group: 0-3mo, 3mo-1y, 1-6y, 6y+"),
    ("weight_kg", "kg", "Patient weight"),
    ("weight_source", "", "measured / chart_same_day / chart_72h / estimated_* / dicom_unknown"),
    ("weight_group", "", "RP 185 body group (with 80-110kg, 110kg+)"),
    ("height_m", "m", "Patient height"),
    ("indication", "", "Clinical indication code (not collected yet)"),
    ("exam_code", "", "Local nomenclator code"),
    ("edrl_exam", "", "RP 185 exam code"),
    ("ndrl_exam", "", "National DRL exam code (OMS 838/2025)"),
    ("projection", "", "Radiography projection"),
    ("contrast", "", "CT contrast: N, C, NC, MF"),
    ("series", "1", "Series number within the study (CT)"),
    ("protocol", "", "Acquisition protocol"),
    ("ctdivol", "mGy", "CTDIvol of the series"),
    ("phantom", "cm", "CTDI phantom: 16 or 32"),
    ("dlp", "mGy.cm", "DLP of the series"),
    ("scan_length", "mm", "Scan length"),
    ("kvp", "kV", "Tube voltage"),
    ("mas", "mA.s", "Tube current-time product"),
    ("effective_mas", "mA.s", "mAs / pitch (CT)"),
    ("pitch", "1", "Pitch factor"),
    ("tcm", "", "Tube current modulation"),
    ("reconstruction", "", "FBP / IR / DLIR (not collected yet)"),
    ("ssde", "mGy", "Size-specific dose estimate, when the device reports it"),
    ("pka", "mGy.cm2", "Kerma-area product (DAP)"),
    ("fluoro_time", "s", "Fluoroscopy time"),
    ("kae", "mGy", "Entrance air kerma (not collected yet)"),
    ("exposures", "1", "Exposures summed in this row"),
    ("check_dlp", "", "ok / fail / n.a.: CTDIvol x length vs DLP within 10 %"),
    ("check_phantom", "", "ok / warn (32 cm phantom on a head exam) / n.a."),
    ("weight_missing", "", "true when no weight is recorded"),
]
FIELDS = [d[0] for d in DICTIONARY]


def month_range(month: str) -> tuple[dt.datetime, dt.datetime]:
    """'2026-10' -> [2026-10-01, 2026-11-01)."""
    y, m = (int(x) for x in month.split("-"))
    start = dt.datetime(y, m, 1)
    end = dt.datetime(y + (m == 12), m % 12 + 1, 1)
    return start, end


def rows(session: Session, conds: list, secret: bytes, institution: str = "DEFAULT",
         max_age_years: float = 18) -> Iterator[dict]:
    from ..classify import view_code

    _, head_bands = load_bands(session, "rp185-head-age")
    _, weight_bands = load_bands(session, "ext-body-weight")
    max_days = int(max_age_years * 365.25)
    ids = list(session.scalars(
        select(Study.id).where(*conds, Study.age_days.is_not(None), Study.age_days < max_days,
                               Study.modality.in_(["CT", "DX", "CR", "RF", "XA"]))
        .order_by(Study.study_datetime, Study.id)))
    devices = {d.id: d for d in session.scalars(select(Device))}
    physical = {i: (d.merged_into_id or i) for i, d in devices.items()}
    inst = _code(secret, "institution", institution)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = session.execute(
            select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.id.in_(chunk)).order_by(Study.study_datetime, Study.id)).all()
        study_codes: dict[int, dict[str, str]] = defaultdict(dict)
        event_codes: dict[int, dict[str, str]] = defaultdict(dict)
        for sid, eid, system, code in session.execute(
                select(Classification.study_id, Classification.event_id, CodeSystem.key,
                       Code.code).join(Code, Code.id == Classification.code_id)
                .join(CodeSystem).where(Classification.study_id.in_(chunk))):
            (study_codes[sid] if eid is None else event_codes[eid]).setdefault(system, code)
        events: dict[int, list[Event]] = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_(chunk),
                                                     Event.is_localizer.is_not(True))
                                 .order_by(Event.study_id, Event.event_datetime, Event.id)):
            events[e.study_id].append(e)
        for s, p in studies:
            d = devices.get(physical.get(s.device_id)) if s.device_id else None
            hb = band_for_age(head_bands, days=s.age_days)
            wb = band_for_weight(weight_bands, s.weight_kg) if s.weight_kg else None
            sc = study_codes.get(s.id, {})
            base = {
                "survey_version": SURVEY_VERSION, "institution": inst,
                "device": device_code(secret, d),
                "manufacturer": d.manufacturer if d else None,
                "model": d.model_name if d else None, "modality": s.modality,
                "period": s.study_datetime.strftime("%Y-%m") if s.study_datetime else None,
                "patient": patient_code(secret, p),
                "sex": p.sex if p else None, **coarse_age(s.age_days),
                "head_age_group": hb.key if hb else None, "weight_kg": s.weight_kg,
                "weight_source": s.weight_source.value if s.weight_source else None,
                "weight_group": wb.key if wb else None, "height_m": s.height_m,
                "indication": None, "contrast": s.contrast_mode,
                "reconstruction": None, "kae": None,
                "weight_missing": not s.weight_kg,
            }
            evs = events.get(s.id, [])
            if s.modality == "CT":
                for n, e in enumerate(evs, 1):
                    yield _ct_row(base, event_codes.get(e.id) or sc, n, e)
            elif s.modality in EXPOSURE_EXAM:
                groups: dict[tuple, list[Event]] = defaultdict(list)
                for e in evs:
                    ec = event_codes.get(e.id, {})
                    groups[(ec.get("ro-nom-rx"), ec.get(EUROPEAN), ec.get(NATIONAL),
                            view_code(e.view, e.protocol))].append(e)
                for (nom, eu, nat, view), g in groups.items():
                    yield _sum_row(base, nom, eu, nat, view, g)
            else:
                yield _sum_row(base, sc.get("ro-nom-rx"), sc.get(EUROPEAN), sc.get(NATIONAL),
                               None, evs)
        session.expunge_all()


def _empty(base: dict) -> dict:
    return {**{f: None for f in FIELDS}, **base}


def _ct_row(base: dict, sc: dict, n: int, e: Event) -> dict:
    r = _empty(base)
    eu = sc.get(EUROPEAN)
    check = "n.a."
    if e.ctdivol and e.scan_length_mm and e.dlp:
        expected = e.ctdivol * e.scan_length_mm / 10
        check = "ok" if abs(expected - e.dlp) <= 0.1 * e.dlp else "fail"
    phantom = "n.a."
    if e.ct_phantom_cm:
        phantom = "warn" if (eu in HEAD_CODES and e.ct_phantom_cm == 32) else "ok"
    r.update({"exam_code": sc.get("ro-nom-ct"), "edrl_exam": eu, "ndrl_exam": sc.get(NATIONAL),
              "series": n, "protocol": e.protocol, "ctdivol": _r(e.ctdivol, 3),
              "phantom": e.ct_phantom_cm, "dlp": _r(e.dlp, 3),
              "scan_length": _r(e.scan_length_mm, 1), "kvp": e.kvp, "mas": _r(e.mas, 2),
              "effective_mas": _r(e.mas / e.pitch, 2) if e.mas and e.pitch else None,
              "pitch": e.pitch, "tcm": e.tcm, "ssde": _r(e.ssde, 3), "exposures": 1,
              "check_dlp": check, "check_phantom": phantom})
    return r


def _sum_row(base, nom, eu, nat, view, evs: list[Event]) -> dict:
    r = _empty(base)

    def total(attr, factor=1.0):
        v = [getattr(e, attr) for e in evs if getattr(e, attr) is not None]
        return _r(sum(v) * factor, 4) if v else None

    kvp = [e.kvp for e in evs if e.kvp is not None]
    r.update({"exam_code": nom, "edrl_exam": eu, "ndrl_exam": nat, "projection": view,
              "protocol": evs[0].protocol if evs else None,
              "kvp": _r(sum(kvp) / len(kvp), 1) if kvp else None, "mas": total("mas"),
              "pka": total("dap", 1000.0), "fluoro_time": total("fluoro_time_s"),
              "exposures": len(evs), "check_dlp": "n.a.", "check_phantom": "n.a."})
    return r
