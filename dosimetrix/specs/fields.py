"""The built-in fields and transforms a spec can name.

A field is ``fn(row) -> value`` (``extract.Row``). Anything that identifies a person is marked
``identifying=True``: a spec that uses one needs the ``pid`` role whatever its ``access`` says, and
the run is audited as such. List them all with ``dosimetrix export fields``.
"""

from __future__ import annotations

import datetime as dt

from .. import nationalid
from ..patient_age import format_age
from .registry import field, transform

# ----------------------------------------------------------------------------- study


def _study(name: str, attr: str, unit: str = "", doc: str = "", nd: int | None = None):
    def fn(r):
        v = getattr(r.s, attr)
        return round(v, nd) if nd is not None and isinstance(v, float) else v
    fn.__doc__ = doc or f"study.{attr}"
    field(name, unit=unit)(fn)


for _n, _a, _u, _d, _nd in [
    ("study.id", "id", "", "Dosimetrix's own study number", None),
    ("study.date", "study_datetime", "", "Date and time of the study", None),
    ("study.modality", "modality", "", "DICOM modality (PT is stored for PET/CT, NM for SPECT/CT)", None),
    ("study.description", "study_description", "", "Study description from the header", None),
    ("study.procedure", "procedure_name", "", "Procedure name of the request, when known", None),
    ("study.age_days", "age_days", "d", "Age at the study, in days", None),
    ("study.weight_kg", "weight_kg", "kg", "Weight, reported or estimated (see weight_source)", 1),
    ("study.pregnant", "pregnant", "", "Pregnancy recorded for the study (true, false or unknown)", None),
    ("study.gestation_weeks", "gestation_weeks", "weeks", "Weeks of pregnancy, when recorded", None),
    ("study.indication", "indication_text", "", "Clinical indication, free text of the request", None),
    ("study.glandularity_pct", "glandularity_pct", "%", "Breast glandularity, typed in per study", None),
    ("study.total_dlp", "total_dlp", "mGy.cm", "CT: dose-length product of the study", 1),
    ("study.ctdivol_max", "ctdivol_max", "mGy", "CT: the highest CTDIvol of the study", 2),
    ("study.total_dap", "total_dap", "Gy.cm2", "Dose-area product of the study", 4),
    ("study.total_rp_dose", "total_rp_dose", "Gy", "Air kerma at the reference point of the study", 4),
    ("study.fluoro_time_s", "total_fluoro_time", "s", "Fluoroscopy time of the study", 1),
    ("study.activity_mbq", "admin_activity_mbq", "MBq", "Administered activity (nuclear medicine)", 1),
    ("study.agd_left", "agd_left", "mGy", "Mean glandular dose, left breast", 3),
    ("study.agd_right", "agd_right", "mGy", "Mean glandular dose, right breast", 3),
    ("study.n_events", "n_events", "", "Irradiation events in the study", None),
]:
    _study(_n, _a, _u, _d, _nd)


@field("study.height_cm", unit="cm")
def height_cm(r):
    "Height in centimetres."
    return round(r.s.height_m * 100) if r.s.height_m else None


@field("study.accession", identifying=True)
def accession(r):
    "The accession number: links the row to a request, so it is identifying."
    return None if r.s.accession_is_hashed else r.s.accession


# ----------------------------------------------------------------------------- patient, age


@field("patient.sex")
def sex(r):
    "M, F, O or empty."
    return r.p.sex if r.p else None


@field("patient.pseudo_id")
def pseudo_id(r):
    "The program's own pseudonym (P-XXXXX-XXXXX): not reversible without the database."
    return r.p.pseudo_id if r.p else None


@field("patient.name", identifying=True)
def patient_name(r):
    "The patient's name."
    return r.p.name if r.p else None


@field("patient.id", identifying=True)
def patient_id(r):
    "The patient ID, a CNP when the site uses it."
    return r.p.patient_id if r.p else None


@field("patient.birth_date", identifying=True)
def birth_date(r):
    "The exact birth date."
    return r.p.birth_date if r.p else None


@field("patient.cnp7", identifying=True)
def cnp7(r):
    """First 7 digits of the CNP (sex/century and birth date): from the stored ID when it is a
    valid CNP, else rebuilt from the recorded sex and birth date. Partial, but still personal."""
    pid = r.p.patient_id if r.p else None
    if pid and nationalid.ro_cnp(pid):
        return pid.strip()[:7]
    sx, born = (r.p.sex, r.p.birth_date) if r.p else (None, None)
    if sx in ("M", "F") and born:
        century = {18: 3, 19: 1, 20: 5}.get(born.year // 100)
        if century is not None:
            return f"{century + (1 if sx == 'F' else 0)}{born:%y%m%d}"
    return None


@field("age.display", unit="")
def age_display(r):
    "Age as the screens show it: days below a month, months up to 2 years, then years."
    return format_age(r.s.age_days) if r.s.age_days is not None else None


@field("age.years")
def age_years(r):
    "Age in years: one decimal below 5 years, whole years after."
    d = r.s.age_days
    if d is None:
        return None
    y = d / 365.25
    return round(y, 1) if y < 5 else int(y)


@field("age.coarse")
def age_coarse(r):
    """Age as the national survey plan allows: days below 1 year, months up to 24 months, whole
    years after. Finer values would narrow down the birth date."""
    d = r.s.age_days
    if d is None:
        return None
    if d < 365:
        return f"{d} d"
    m = int(d / 30.4375)
    return f"{m} mo" if m < 24 else f"{int(d / 365.25)} y"


@field("pregnancy.text")
def pregnancy(r):
    """"Da, N sapt" / "Nu" for women of 12 to 55 years when it is recorded, else empty."""
    d = r.s.age_days
    if not r.p or r.p.sex != "F" or d is None or not 12 <= d / 365.25 <= 55 or r.s.pregnant is None:
        return None
    if not r.s.pregnant:
        return "Nu"
    return f"Da, {r.s.gestation_weeks} sapt" if r.s.gestation_weeks else "Da"


# ----------------------------------------------------------------------------- device, time


@field("device.name")
def device_name(r):
    "The device's display name (an alias shows the name of the device it is merged into)."
    return r.ctx.names.get(r.s.device_id)


@field("device.manufacturer")
def manufacturer(r):
    "Manufacturer."
    return r.d.manufacturer if r.d else None


@field("device.model")
def model(r):
    "Model name."
    return r.d.model_name if r.d else None


@field("device.serial")
def serial(r):
    "Serial number."
    return r.d.serial_number if r.d else None


@field("device.institution")
def institution(r):
    "Institution name the device reports."
    return r.d.institution_name if r.d else None


@field("time.month")
def month(r):
    "Year and month of the study (YYYY-MM): coarse enough to publish."
    return f"{r.s.study_datetime:%Y-%m}" if r.s.study_datetime else None


@field("time.date")
def date_only(r):
    "Date of the study."
    return r.s.study_datetime.date() if r.s.study_datetime else None


# ----------------------------------------------------------------------------- pseudonyms


@field("pseudo.patient")
def pseudo_patient(r):
    "A keyed code for the patient, stable across re-imports (HMAC with the site secret)."
    from ..export.drl_dataset import patient_code

    return patient_code(r.ctx.secret, r.p)


@field("pseudo.device")
def pseudo_device(r):
    "A keyed code for the device (HMAC with the site secret)."
    from ..export.drl_dataset import device_code

    return device_code(r.ctx.secret, r.d)


# ----------------------------------------------------------------------------- transforms


@transform("round")
def round_(v, nd="2"):
    "round:N, N decimals."
    return round(v, int(nd)) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


@transform("scale")
def scale(v, factor="1"):
    "scale:K, multiply by K (a unit change such as Gy to mGy: scale:1000)."
    return v * float(factor) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


@transform("yesno")
def yesno(v):
    "true to Da, false to Nu."
    return None if v is None else ("Da" if v else "Nu")


@transform("upper")
def upper(v):
    "Upper case."
    return v.upper() if isinstance(v, str) else v


@transform("date")
def date_(v):
    "A date or datetime as YYYY-MM-DD."
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    return v.isoformat() if isinstance(v, dt.date) else v


@transform("datetime")
def datetime_(v):
    "A datetime as YYYY-MM-DD HH:MM."
    return f"{v:%Y-%m-%d %H:%M}" if isinstance(v, dt.datetime) else v
