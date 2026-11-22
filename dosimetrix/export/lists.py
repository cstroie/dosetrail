"""List exports: one row per study, or one row per irradiation event, as CSV or XLSX.

* The selection is the typed query language, the same as the study list and the charts.
* Patients appear as pseudonyms (P-XXXXX-XXXXX). Identifiers (ID, name, birth date) are
  added only on request, and the API allows that only to the ``pid`` role.
* Units are fixed and written in the headers, UCUM style: DAP Gy.cm2, DLP mGy.cm,
  CTDIvol mGy, Ka,r Gy.
* Exam codes are the local nomenclator (study level) and, per event, the nomenclator and
  DRL codes, so a spreadsheet can be filtered by standard exam.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterator
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Classification, Code, CodeSystem, Device, Event, Patient, Study
from ..patient_age import format_age
from .xlsx import XlsxWriter

BATCH = 2000
NOMENCLATORS = ("ro-nom-ct", "ro-nom-rx")
DRL_SYSTEMS = ("ro-drl-ms838-2025", "ec-rp185")


def _f(x, nd=6):
    return round(x, nd) if isinstance(x, float) else x


Col = tuple[str, Callable[[dict], object]]


def _attr(name: str, nd: int | None) -> Callable[[dict], object]:
    if nd:
        return lambda r: _f(getattr(r["e"], name), nd)
    return lambda r: getattr(r["e"], name)

STUDY_COLUMNS: list[Col] = [
    ("Study ID", lambda r: r["s"].id),
    ("Date", lambda r: r["s"].study_datetime),
    ("Modality", lambda r: r["s"].modality),
    ("Device", lambda r: r["device"]),
    ("Origin", lambda r: r["origin"]),
    ("Manufacturer", lambda r: r["d"].manufacturer if r["d"] else None),
    ("Model", lambda r: r["d"].model_name if r["d"] else None),
    ("Accession", lambda r: None if r["s"].accession_is_hashed else r["s"].accession),
    ("Patient", lambda r: r["p"].pseudo_id if r["p"] else None),
    ("Sex", lambda r: r["p"].sex if r["p"] else None),
    ("Age [days]", lambda r: r["s"].age_days),
    ("Age", lambda r: format_age(r["s"].age_days) if r["s"].age_days is not None else None),
    ("Age precision", lambda r: str(r["s"].age_precision.value) if r["s"].age_precision else None),
    ("Weight [kg]", lambda r: r["s"].weight_kg),
    ("Weight source", lambda r: r["s"].weight_source.value if r["s"].weight_source else None),
    ("Weight reported [kg]", lambda r: r["s"].weight_reported_kg),
    ("Height [m]", lambda r: r["s"].height_m),
    ("Study description", lambda r: r["s"].study_description),
    ("Exam code", lambda r: "; ".join(r["exam"])),
    ("Exam", lambda r: "; ".join(r["exam_name"])),
    ("Contrast", lambda r: r["s"].contrast_mode),
    ("Events", lambda r: r["s"].n_events),
    ("DLP total [mGy.cm]", lambda r: _f(r["s"].total_dlp, 3)),
    ("CTDIvol max [mGy]", lambda r: _f(r["s"].ctdivol_max, 3)),
    ("DAP total [Gy.cm2]", lambda r: _f(r["s"].total_dap)),
    ("DAP fluoroscopy [Gy.cm2]", lambda r: _f(r["s"].total_fluoro_dap)),
    ("DAP acquisition [Gy.cm2]", lambda r: _f(r["s"].total_acq_dap)),
    ("Ka,r total [Gy]", lambda r: _f(r["s"].total_rp_dose)),
    ("Fluoroscopy time [s]", lambda r: _f(r["s"].total_fluoro_time, 1)),
    ("AGD left [mGy]", lambda r: _f(r["s"].agd_left, 3)),
    ("AGD right [mGy]", lambda r: _f(r["s"].agd_right, 3)),
]
PID_COLUMNS: list[Col] = [
    ("Patient ID", lambda r: r["p"].patient_id if r["p"] else None),
    ("Patient name", lambda r: r["p"].name if r["p"] else None),
    ("Birth date", lambda r: r["p"].birth_date if r["p"] else None),
]
_EV = [
    ("Event time", "event_datetime", None), ("Event type", "event_type", None),
    ("Protocol", "protocol", None), ("Anatomy", "anatomy", None), ("View", "view", None),
    ("Laterality", "laterality", None), ("Plane", "plane", None),
    ("Localizer", "is_localizer", None),
    ("CTDIvol [mGy]", "ctdivol", 3), ("DLP [mGy.cm]", "dlp", 3),
    ("Phantom [cm]", "ct_phantom_cm", None), ("Scan length [mm]", "scan_length_mm", 1),
    ("Pitch", "pitch", 3), ("Collimation [mm]", "collimation_mm", 2),
    ("Rotation time [s]", "rotation_time_s", 3), ("SSDE [mGy]", "ssde", 3),
    ("SSDE calculated [mGy]", "ssde_calc", 3), ("SSDE method", "ssde_method", None),
    ("kVp [kV]", "kvp", 1), ("mA", "ma", 2), ("mAs", "mas", 3),
    ("Exposure time [s]", "exposure_time_s", 4), ("DAP [Gy.cm2]", "dap", 8),
    ("Ka,r [Gy]", "rp_dose", 8), ("Fluoroscopy time [s]", "fluoro_time_s", 2),
    ("Pulse rate [/s]", "pulse_rate", 2), ("Pulses", "n_pulses", None),
    ("Cu filter [mm]", "filter_cu_mm", 2), ("Al filter [mm]", "filter_al_mm", 2),
    ("SID [mm]", "sid_mm", 1), ("Field width [mm]", "field_w_mm", 1),
    ("Field height [mm]", "field_h_mm", 1), ("Primary angle", "primary_angle", 1),
    ("Secondary angle", "secondary_angle", 1), ("EI", "exposure_index", 2),
    ("Target EI", "target_exposure_index", 2), ("DI", "deviation_index", 2),
    ("AGD [mGy]", "agd", 3), ("Compression [mm]", "compression_thickness_mm", 1),
    ("Exposure control", "exposure_control_mode", None),
]
EVENT_COLUMNS: list[Col] = [
    *[c for c in STUDY_COLUMNS if c[0] in (
        "Study ID", "Date", "Modality", "Device", "Origin", "Accession", "Patient", "Sex",
        "Age [days]", "Weight [kg]", "Study description")],
    ("Exam code", lambda r: r["ev_exam"]),
    ("DRL exam (national)", lambda r: r["ev_drl"].get("ro-drl-ms838-2025")),
    ("DRL exam (European)", lambda r: r["ev_drl"].get("ec-rp185")),
    *[(h, _attr(a, nd)) for h, a, nd in _EV],
]


def columns(level: str, include_pid: bool) -> list[Col]:
    cols = list(STUDY_COLUMNS if level == "study" else EVENT_COLUMNS)
    if include_pid:
        i = [c[0] for c in cols].index("Patient") + 1
        cols[i:i] = PID_COLUMNS
    return cols


def _codes(session: Session, ids: list[int]):
    """study id -> [(code, meaning)] at study level; event id -> {system: code}."""
    study_codes: dict[int, list[tuple[str, str]]] = defaultdict(list)
    event_codes: dict[int, dict[str, str]] = defaultdict(dict)
    study_drl: dict[int, dict[str, str]] = defaultdict(dict)
    rows = session.execute(
        select(Classification.study_id, Classification.event_id, CodeSystem.key, Code.code,
               Code.meaning)
        .join(Code, Code.id == Classification.code_id).join(CodeSystem)
        .where(Classification.study_id.in_(ids)).order_by(Classification.id))
    for sid, eid, system, code, meaning in rows:
        if eid is None:
            if system in NOMENCLATORS:
                study_codes[sid].append((code, meaning))
            elif system in DRL_SYSTEMS:
                study_drl[sid][system] = code
        else:
            event_codes[eid][system] = code
    return study_codes, event_codes, study_drl


def rows(session: Session, conds: list, level: str = "study", include_pid: bool = False,
         device_names: dict[int, str] | None = None, physical: dict[int, int] | None = None,
         limit: int | None = None) -> Iterator[list]:
    """Rows (lists of cell values) in the order of ``columns(level, include_pid)``."""
    cols = columns(level, include_pid)
    ids = list(session.scalars(select(Study.id).where(*conds)
                               .order_by(Study.study_datetime, Study.id).limit(limit)))
    devices = {d.id: d for d in session.scalars(select(Device))}
    physical = physical or {i: (d.merged_into_id or i) for i, d in devices.items()}
    names = device_names or {i: (devices[p].display_name or devices[p].station_name
                                 or f"Device {p}") for i, p in physical.items()}
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = session.execute(
            select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.id.in_(chunk)).order_by(Study.study_datetime, Study.id)).all()
        study_codes, event_codes, study_drl = _codes(session, chunk)
        events: dict[int, list[Event]] = defaultdict(list)
        if level == "event":
            for e in session.scalars(select(Event).where(Event.study_id.in_(chunk))
                                     .order_by(Event.study_id, Event.event_datetime, Event.id)):
                events[e.study_id].append(e)
        for s, p in studies:
            d = devices.get(physical.get(s.device_id, s.device_id)) if s.device_id else None
            base = {"s": s, "p": p, "d": d,
                    "device": names.get(s.device_id) if s.device_id else None,
                    "origin": None if d is None else ("local" if d.is_local else "external"),
                    "exam": [c for c, _ in study_codes.get(s.id, [])],
                    "exam_name": [m for _, m in study_codes.get(s.id, [])]}
            if level == "study":
                yield [fn(base) for _, fn in cols]
                continue
            for e in events.get(s.id, []):
                codes = event_codes.get(e.id, {})
                nom = next((codes[k] for k in NOMENCLATORS if k in codes), None)
                # radiography exposures carry their own codes; CT series and fluoroscopy
                # events take the study's
                drl = {k: codes[k] for k in DRL_SYSTEMS if k in codes} or study_drl.get(s.id, {})
                r = {**base, "e": e, "ev_exam": nom or "; ".join(base["exam"]) or None,
                     "ev_drl": drl}
                yield [fn(r) for _, fn in cols]
        session.expunge_all()


def safe_text(v):
    """Spreadsheet-safe text: a cell starting with = + - @ (e.g. "- - - - -" descriptions
    or a crafted DICOM field) is read as a formula by Excel; prefix it with an apostrophe.
    Numbers are left alone."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv_value(v):
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, bool):
        return "1" if v else "0"
    return "" if v is None else safe_text(v)


def write_csv(fh: IO[str], level: str, include_pid: bool, data: Iterator[list]) -> int:
    w = csv.writer(fh, lineterminator="\r\n")
    w.writerow([h for h, _ in columns(level, include_pid)])
    n = 0
    for row in data:
        w.writerow([_csv_value(v) for v in row])
        n += 1
    return n


def write_xlsx(target, level: str, include_pid: bool, data: Iterator[list],
               meta: list[tuple[str, object]] | None = None) -> int:
    header = [h for h, _ in columns(level, include_pid)]
    widths = [max(9, min(len(h) + 2, 28)) for h in header]
    n = 0
    with XlsxWriter(target) as wb:
        ws = wb.sheet("Studies" if level == "study" else "Events", header, widths)
        for row in data:
            if ws.rows >= 1_048_576:
                ws = wb.sheet(f"{ws.name} ({len(wb.sheets) + 1})", header, widths)
            ws.row(row)
            n += 1
        if meta:
            info = wb.sheet("About", ["Item", "Value"], [24, 80])
            for k, v in [*meta, ("Rows", n)]:
                info.row([k, v])
    return n
