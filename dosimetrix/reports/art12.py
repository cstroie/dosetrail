"""The annual DRL return, Order MS 1255/2016 art. 12.

Every unit sends the individual records of **20 patients examined in February and March**,
for every type of examination and every X-ray or nuclear-medicine device, by 15 April. The
INSP supplies one workbook per modality family (reference/insp-forms/Art12_*.xls); each has a
general-information sheet (the one device the file is about) and one sheet per examination with
the patient and dose columns.

This module fills them. One workbook per family and physical device, one sheet per official
examination sheet, in the official column order:
* **Selection.** Per family, device and sheet: the first 20 studies of the window in time order
  (study date, then id). Deterministic, so a rerun gives the same patients. A sheet with fewer
  than 20 has what exists; the summary says how many are missing.
* **Patient.** The first 7 digits of the CNP (sex/century and birth date: the form asks no more)
  from the stored ID when it is a valid CNP, else built from the recorded sex and birth date;
  age, sex, height (cm), weight (kg), pregnancy (women of 12 to 55 years) and the clinical
  indication. These are personal data: the API gates the export with the ``pid`` role and
  audits it.
* **Dose.** CT: the study's DLP. Radiography: the DAP of the projection (retakes included), and
  the ESAK of a room without a DAP meter (the device's own entrance dose, else the annex 2
  calculation, esak.py) in a last column the official sheet does not have.
  Fluoroscopy: the study's DAP. Mammography: the mean glandular dose of each view. Nuclear
  medicine: the administered activity (one injection split over two studies counts once);
  hybrid: also the CT DLP. The estimated effective dose of nuclear medicine is filled only for
  radiopharmaceuticals that have a coefficient in the table (effdose.py; none ship).

Which national code goes on which sheet is seed data (``seeds/art12_sheets.toml``). Interventional
cardiology and radiology, dental and bone densitometry have their own INSP files and are not
generated yet.
"""

from __future__ import annotations

import datetime as dt
import tomllib
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import nationalid
from ..db.models import Classification, Code, CodeSystem, Device, Event, Patient, Study
from ..export.xlsx import XlsxWriter
from .insp1255 import _shared_administration

PATIENTS_PER_SHEET = 20  # art. 12(1)
SEED = Path(__file__).resolve().parent.parent / "seeds" / "art12_sheets.toml"

COMMON = ["Nr. crt", "CNP (primele 7 cifre)", "Varsta", "Gen", "Inaltime (cm)", "Greutate (kg)"]
PREGNANCY = "Insarcinata Da/Nu / Varsta sarcinii"
INDICATION = "Indicatie clinica"
COLUMNS = {
    "ct": [*COMMON, PREGNANCY, INDICATION, "Nativ/Contrast",
           "DLP total (mGy x cm) per procedura"],
    # the official sheet ends at the DAP; the ESAK column is ours, for rooms without a DAP meter
    "rx": [*COMMON, PREGNANCY, INDICATION, "Proiectie (AP/PA/LAT)", "DAP (Gy x cm2)",
           "ESAK (mGy), fara DAP-metru"],
    "fluoro": [*COMMON, PREGNANCY, INDICATION, "DAP (Gy x cm2)"],
    "mg": [*COMMON, PREGNANCY, "Proiectie (CC/MLO)", "San stang/drept", "MGD (mGy)"],
    "nm": [*COMMON, PREGNANCY, INDICATION, "Radionuclid folosit", "Forma chimica", "Cod proc",
           "Activitatea administrata (MBq)", "Doza efectiva estimata (mSv)"],
    "nmh": [*COMMON, PREGNANCY, INDICATION, "Radionuclid folosit", "Forma chimica", "Cod proc",
            "Activitatea administrata (MBq)", "DLP (mGy x cm)"],
}
CONTRAST = {"TEP"}  # contrast-enhanced CT codes without the SC suffix


@dataclass
class SheetData:
    name: str
    codes: list[str]
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    patients: int = 0  # studies on the sheet (<= PATIENTS_PER_SHEET)
    available: int = 0  # studies of the window before the cut


@dataclass
class Workbook:
    family: str
    title: str
    file: str
    device_id: int
    device_name: str
    info: dict
    sheets: list[SheetData]

    @property
    def filename(self) -> str:
        safe = "".join(c if c.isalnum() else "_" for c in self.device_name).strip("_")
        # the id keeps two devices of the same name (aliases, twin rooms) apart in one zip
        return f"{self.file}_{safe or 'device'}_{self.device_id}.xlsx"


def seed() -> list[dict]:
    return tomllib.loads(SEED.read_text(encoding="utf-8"))["family"]


def window(year: int) -> tuple[dt.datetime, dt.datetime]:
    """February and March of the year."""
    return dt.datetime(year, 2, 1), dt.datetime(year, 4, 1)


def cnp7(patient_id: str | None, sex: str | None, birth: dt.date | None) -> str:
    """First 7 CNP digits: from a valid stored CNP, else rebuilt from sex and birth date."""
    if patient_id and nationalid.ro_cnp(patient_id):
        return patient_id.strip()[:7]
    if sex in ("M", "F") and birth:
        century = {18: 3, 19: 1, 20: 5}.get(birth.year // 100)
        if century is None:
            return ""
        return f"{century + (1 if sex == 'F' else 0)}{birth:%y%m%d}"
    return ""


def _age(days: int | None) -> float | int | str:
    if days is None:
        return ""
    years = days / 365.25
    return round(years, 1) if years < 5 else int(years)


def _pregnancy(sex: str | None, days: int | None, pregnant: bool | None,
               weeks: int | None) -> str:
    if sex != "F" or days is None or not 12 <= days / 365.25 <= 55 or pregnant is None:
        return ""
    return (f"Da, {weeks} sapt" if weeks else "Da") if pregnant else "Nu"


def _split_meaning(meaning: str) -> tuple[str, str]:
    """'Scintigrafie renala — Tc-99m / DMSA' -> ('Tc-99m', 'DMSA')."""
    _, _, tail = meaning.partition(" — ")
    nuc, _, form = tail.partition(" / ")
    return nuc.strip(), form.strip()


def _studies(session: Session, fam: dict, start: dt.datetime, end: dt.datetime,
             conds: list) -> list[dict]:
    """One dict per (study, code) of the family: the patient, the dose and the exam's parts."""
    q = (select(Classification.study_id, Classification.event_id, Code.code, Code.meaning,
                Study.study_datetime, Study.device_id, Study.age_days, Study.weight_kg,
                Study.height_m, Study.pregnant, Study.gestation_weeks, Study.indication_text,
                Study.total_dlp, Study.total_dap, Study.admin_activity_mbq,
                Patient.patient_id, Patient.sex, Patient.birth_date,
                Event.dap, Event.agd, Event.view, Event.laterality, Event.esd, Event.esak_calc)
         .join(Code, Code.id == Classification.code_id)
         .join(CodeSystem, CodeSystem.id == Code.system_id)
         .join(Study, Study.id == Classification.study_id)
         .outerjoin(Patient, Patient.id == Study.patient_id)
         .outerjoin(Event, Event.id == Classification.event_id)
         .where(CodeSystem.key == fam["system"], Study.modality.in_(fam["modalities"]),
                Study.study_datetime >= start, Study.study_datetime < end, *conds))
    grouped: dict[tuple, dict] = {}
    kind = fam["kind"]
    for r in session.execute(q).all():
        key = (r.study_id, r.code, r.event_id) if kind == "mg" else (r.study_id, r.code)
        g = grouped.get(key)
        if g is None:
            g = grouped[key] = {
                "study": r.study_id, "when": r.study_datetime, "device": r.device_id,
                "code": r.code, "meaning": r.meaning, "age_days": r.age_days,
                "weight": r.weight_kg, "height": r.height_m, "pregnant": r.pregnant,
                "weeks": r.gestation_weeks, "indication": r.indication_text,
                "patient_id": r.patient_id, "sex": r.sex, "birth": r.birth_date,
                "dlp": r.total_dlp, "dap": None, "esak": None, "agd": r.agd, "view": r.view,
                "side": r.laterality, "activity": r.admin_activity_mbq,
                "study_dap": r.total_dap, "event_level": r.event_id is not None}
        if r.event_id is not None and r.dap is not None and kind != "mg":
            g["dap"] = (g["dap"] or 0.0) + r.dap  # retakes included
        entrance = r.esd if r.esd is not None else r.esak_calc
        if r.event_id is not None and entrance is not None and kind != "mg":
            g["esak"] = (g["esak"] or 0.0) + entrance
    return list(grouped.values())


def _row(kind: str, g: dict, n: int, coefficients) -> list:
    dose = g["dap"] if g["event_level"] else g["study_dap"]
    height = round(g["height"] * 100) if g["height"] else ""
    head = [n, cnp7(g["patient_id"], g["sex"], g["birth"]), _age(g["age_days"]),
            g["sex"] or "", height, round(g["weight"], 1) if g["weight"] else "",
            _pregnancy(g["sex"], g["age_days"], g["pregnant"], g["weeks"])]
    ind = g["indication"] or ""
    if kind == "ct":
        contrast = g["code"].endswith("SC") or g["code"] in CONTRAST
        return [*head, ind, "contrast" if contrast else "nativ", _num(g["dlp"], 1)]
    if kind == "rx":
        _, _, proj = g["meaning"].partition(" — ")
        return [*head, ind, proj or g["view"] or "", _num(dose, 4), _num(g["esak"], 3)]
    if kind == "fluoro":
        return [*head, ind, _num(dose, 4)]
    if kind == "mg":
        side = {"R": "drept", "L": "stang"}.get((g["side"] or "")[:1].upper(), g["side"] or "")
        return [*head, g["view"] or "", side, _num(g["agd"], 3)]
    nuc, form = _split_meaning(g["meaning"])
    base = [*head, ind, nuc, form, g["code"], _num(g["activity"], 1)]
    if kind == "nmh":
        return [*base, _num(g["dlp"], 1)]
    coef = coefficients(nuc, form, g["age_days"])
    msv = g["activity"] * coef if coef is not None and g["activity"] is not None else None
    return [*base, _num(msv, 2)]


def _num(v, digits: int):
    return round(v, digits) if v is not None else ""


def build(session: Session, year: int, conds: list | None = None,
          coefficients=None) -> list[Workbook]:
    """The workbooks of the year: one per family and physical device with examinations."""
    start, end = window(year)
    conds = conds or []
    if coefficients is None:
        from ..effdose import load

        coefficients = load(session)
    devs = {d.id: d for d in session.scalars(select(Device))}

    def physical(i: int | None) -> Device | None:
        d = devs.get(i)
        return devs.get(d.merged_into_id, d) if d is not None and d.merged_into_id else d

    out: list[Workbook] = []
    for fam in seed():
        by_code = {c: s["name"] for s in fam["sheet"] for c in s["codes"]}
        cols = COLUMNS[fam["kind"]]
        rows = _studies(session, fam, start, end, conds)
        if fam["kind"] in ("nm", "nmh"):  # one injection split over two studies counts once
            shared = _shared_administration(session, {g["study"] for g in rows})
            rows = [g for g in rows if g["study"] not in shared]
        per_dev: dict[int, dict[str, list[dict]]] = {}
        for g in rows:
            sheet, dev = by_code.get(g["code"]), physical(g["device"])
            if sheet is None or dev is None:
                continue
            per_dev.setdefault(dev.id, {}).setdefault(sheet, []).append(g)
        for dev_id, sheets in sorted(per_dev.items()):
            d = devs[dev_id]
            sd = []
            for s in fam["sheet"]:
                units = sorted(sheets.get(s["name"], []),
                               key=lambda g: (g["when"], g["study"], g["code"]))
                studies = list(dict.fromkeys(g["study"] for g in units))
                keep = set(studies[:PATIENTS_PER_SHEET])
                chosen = [g for g in units if g["study"] in keep]
                sd.append(SheetData(s["name"], s["codes"], cols,
                                    [_row(fam["kind"], g, i, coefficients)
                                     for i, g in enumerate(chosen, 1)],
                                    len(keep), len(studies)))
            name = d.display_name or d.station_name or f"Device {d.id}"
            info = {"Institutia": d.institution_name or "", "Departament": d.department_name or "",
                    "Tip si model echipament": d.model_name or "",
                    "Producator": d.manufacturer or "", "Serie": d.serial_number or "",
                    "Anul": year,
                    "Perioada": f"{start:%Y-%m-%d} – {end - dt.timedelta(days=1):%Y-%m-%d}",
                    "Selectie": f"primii {PATIENTS_PER_SHEET} pacienți în ordine cronologică "
                                f"per examinare (art. 12 alin. 1)"}
            out.append(Workbook(fam["key"], fam["title"], fam["file"], d.id, name, info, sd))
    return out


def summary(books: list[Workbook]) -> list[dict]:
    """For the Reports page: per workbook, per sheet, patients found of the 20 asked."""
    return [{"family": b.family, "title": b.title, "device_id": b.device_id,
             "device": b.device_name, "file": b.filename,
             "sheets": [{"name": s.name, "patients": s.patients, "available": s.available,
                         "missing": max(PATIENTS_PER_SHEET - s.patients, 0)}
                        for s in b.sheets if s.available]} for b in books]


def write_workbook(book: Workbook, target: IO[bytes], extra: dict[str, object] | None = None
                   ) -> None:
    with XlsxWriter(target) as wb:
        about = wb.sheet("INFORMATII GENERALE", ["Câmp", "Valoare"], [38, 60])
        for k, v in {**book.info, **(extra or {})}.items():
            about.row([k, v])
        for s in book.sheets:
            ws = wb.sheet(s.name[:31], s.columns, [8, 16, 8, 6, 10, 10, 18, 30]
                          + [16] * (len(s.columns) - 8))
            for r in s.rows:
                ws.row(r)


def write_zip(books: list[Workbook], target: IO[bytes], extra: dict[str, object] | None = None
              ) -> None:
    import io

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for b in books:
            buf = io.BytesIO()
            write_workbook(b, buf, extra)
            z.writestr(b.filename, buf.getvalue())
