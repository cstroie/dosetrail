"""DICOM object -> ParsedObject (study / patient / device / events in storage units).

Supported:
* X-Ray Radiation Dose SR, CT (TID 10011) and projection X-ray (TID 10001), recognised by
  content (document title 113701 + template) whatever the SOP class: some scanners send the
  dose report inside a Secondary Capture object;
* DX / CR / MG / RF / XA image headers (one event per image, event UID = SOP Instance UID,
  as OpenREM did, so re-sent images match imported history);
* NM / PET image headers: one event per radiopharmaceutical administration (activity,
  agent, radionuclide), the same for every image of the administration;
* CT image headers carry no dose but name the staff: they only fill the operator and the
  performing physician of a study already known (kind ``staff``).
Everything else (text reports, examination reports, images of other modalities) is ignored.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field

from ..dicom.sr import Node, code_in, parse_dt, tree
from ..patient_age import parse_dicom_age
from . import dosepage

RDSR_TITLE = "113701"  # X-Ray Radiation Dose Report
HEADER_SOP = {
    "1.2.840.10008.5.1.4.1.1.1": "CR",
    "1.2.840.10008.5.1.4.1.1.1.1": "DX",
    "1.2.840.10008.5.1.4.1.1.1.1.1": "DX",  # for processing
    "1.2.840.10008.5.1.4.1.1.1.2": "MG",
    "1.2.840.10008.5.1.4.1.1.1.2.1": "MG",
    "1.2.840.10008.5.1.4.1.1.12.1": "XA",
    "1.2.840.10008.5.1.4.1.1.12.2": "RF",
}
CT_IMAGE_SOP = {"1.2.840.10008.5.1.4.1.1.2", "1.2.840.10008.5.1.4.1.1.2.1",
                "1.2.840.10008.5.1.4.1.1.2.2"}
NM_SOP = {"1.2.840.10008.5.1.4.1.1.20": "NM", "1.2.840.10008.5.1.4.1.1.128": "PT",
          "1.2.840.10008.5.1.4.1.1.128.1": "PT", "1.2.840.10008.5.1.4.1.1.130": "PT"}
FOR_PROCESSING = {"1.2.840.10008.5.1.4.1.1.1.1.1", "1.2.840.10008.5.1.4.1.1.1.2.1"}

CT_TYPES = [
    ({"113805"}, {"constant angle acquisition"}, "localizer"),
    ({"113804"}, {"sequenced acquisition"}, "axial"),
    ({"P5-08001", "116152004"}, {"spiral acquisition"}, "spiral"),
    ({"113806"}, {"stationary acquisition"}, "stationary"),
    ({"113807"}, {"free acquisition"}, "free"),
    ({"702569007"}, {"cone beam acquisition"}, "cone_beam"),
]
PROJ_FLUORO = ({"P5-06000", "44491008"}, {"fluoroscopy"})
PROJ_ACQ = ({"113611", "113612", "113613"},
            {"stationary acquisition", "stepping acquisition", "rotational acquisition"})


@dataclass
class ParsedObject:
    kind: str  # ct_rdsr | proj_rdsr | header | ct_header | dose_page | nm_header | staff | ignored
    sop_instance_uid: str
    sop_class_uid: str
    reason: str = ""
    study: dict = field(default_factory=dict)
    patient: dict = field(default_factory=dict)
    device: dict = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------- helpers


def _s(v, n: int = 255) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)) or v.__class__.__name__ == "MultiValue":
        v = "\\".join(str(x) for x in v)
    v = str(v).strip()
    return v[:n] or None


def _f(v) -> float | None:
    try:
        if v is None or v == "":
            return None
        if v.__class__.__name__ == "MultiValue":
            v = v[0]
        return float(v)
    except (TypeError, ValueError):
        return None


def _dgy_to_mgy(v) -> float | None:
    x = _f(v)
    return x * 100 if x is not None else None


def _date(v) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(v)[:8], "%Y%m%d").date() if v else None
    except ValueError:
        return None


def _birth_date(v) -> dt.date | None:
    """PatientBirthDate, unless it is an "unknown" placeholder: systems that cannot store an
    empty date write 1753-01-01 (SQL Server's minimum) or 1900-01-01, which would make the
    patient 120-270 years old. Then the DICOM Patient Age is used instead."""
    d = _date(v)
    return d if d is not None and d.year >= 1901 else None


def _datetime(d, t) -> dt.datetime | None:
    if not d:
        return None
    return parse_dt(f"{str(d)[:8]}{(str(t) if t else '').replace(':', '')}")


def _indication(ds) -> str | None:
    """The request's reason, where the RIS / modality wrote one: Reason for the Requested
    Procedure (0040,1002), Reason for Study (0032,1030), Admitting Diagnoses (0008,1080),
    also inside the Request Attributes Sequence."""
    seqs = [ds, *(ds.get("RequestAttributesSequence") or [])]
    for item in seqs:
        for kw in ("ReasonForTheRequestedProcedure", "ReasonForStudy",
                   "AdmittingDiagnosesDescription"):
            v = _s(item.get(kw), 1024)
            if v:
                return v
    return None


def _header(ds) -> tuple[dict, dict, dict]:
    study_dt = _datetime(ds.get("StudyDate"), ds.get("StudyTime"))
    study = {
        "study_uid": _s(ds.get("StudyInstanceUID"), 64),
        "accession": _s(ds.get("AccessionNumber"), 64),
        "study_datetime": study_dt,
        "study_description": _s(ds.get("StudyDescription")),
        "requested_procedure": _s(ds.get("RequestedProcedureDescription")),
        "procedure_name": _s(ds.get("PerformedProcedureStepDescription")),
        "performing_physician": _s(ds.get("PerformingPhysicianName")),
        "operator_name": _s(ds.get("OperatorsName")),
        "referring_physician": _s(ds.get("ReferringPhysicianName")),
        "modality": _s(ds.get("Modality"), 16),
        "weight_kg": _f(ds.get("PatientWeight")) or None,
        "height_m": _f(ds.get("PatientSize")) or None,
        "dicom_age": _s(ds.get("PatientAge"), 8),
        "indication_text": _indication(ds),
    }
    study["indication_source"] = "dicom" if study["indication_text"] else None
    patient = {
        "patient_id": _s(ds.get("PatientID"), 64),
        "issuer": _s(ds.get("IssuerOfPatientID"), 64),
        "name": _s(ds.get("PatientName")),
        "birth_date": _birth_date(ds.get("PatientBirthDate")),
        "sex": (_s(ds.get("PatientSex"), 1) or None),
    }
    device = {
        "manufacturer": _s(ds.get("Manufacturer"), 128),
        "model_name": _s(ds.get("ManufacturerModelName"), 128),
        "serial_number": _s(ds.get("DeviceSerialNumber"), 64),
        "station_name": _s(ds.get("StationName"), 64),
        "institution_name": _s(ds.get("InstitutionName")),
        "department_name": _s(ds.get("InstitutionalDepartmentName")),
        "software_versions": _s(ds.get("SoftwareVersions")),
        "gantry_id": _s(ds.get("GantryID"), 64),
        "device_observer_uid": None,
    }
    return study, patient, device


def _observer(root: Node, device: dict) -> None:
    """Device observer context (TID 1004) overrides header identity where present."""
    uid = root.text("121012", deep=False)
    if uid:
        device["device_observer_uid"] = uid[:64]
    for code, key, n in (("121014", "manufacturer", 128), ("121015", "model_name", 128),
                         ("121016", "serial_number", 64)):
        v = root.text(code, deep=False)
        if v and v != "-":
            device[key] = v[:n]
    if not device.get("station_name"):
        name = root.text("121013", deep=False)
        if name:
            device["station_name"] = name[:64]


# TID 1020 person participant roles (DCM 113850 / 113851)
AUTHORIZING, ADMINISTERING = "113850", "113851"


def _staff(root: Node, study: dict) -> None:
    """Who gave the dose, when the header names nobody: the dose report's person participant
    in the role "Irradiation Administering" (else its person observer) is the operator; the
    one "Irradiation Authorizing" is the performing physician."""
    for p in (n for n in root.iter() if n.code == "113870" and n.value):
        role = p.coded("113875")
        name = str(p.value).strip()[:255]
        if role and role[0] == ADMINISTERING and not study.get("operator_name"):
            study["operator_name"] = name
        elif role and role[0] == AUTHORIZING and not study.get("performing_physician"):
            study["performing_physician"] = name
    if not study.get("operator_name"):
        observer = root.text("121008")
        if observer:
            study["operator_name"] = observer[:255]


def _patient_characteristics(root: Node, study: dict) -> None:
    """RDSR patient characteristics: weight (LOINC 29463-7) and height (8302-2)."""
    pc = root.find("121118")
    if pc is None:
        return
    if not study.get("weight_kg"):
        study["weight_kg"] = pc.num("29463-7", "kg") or None
    if not study.get("height_m"):
        study["height_m"] = pc.num("8302-2", "m") or None


# ----------------------------------------------------------------------------- classify


def parse(ds) -> ParsedObject:
    sop_class = str(ds.get("SOPClassUID", ""))
    sop_uid = str(ds.get("SOPInstanceUID", ""))
    if ds.get("ContentSequence") and ds.get("ConceptNameCodeSequence"):
        title = str(ds.ConceptNameCodeSequence[0].get("CodeValue", ""))
        if title != RDSR_TITLE:
            return ParsedObject("ignored", sop_uid, sop_class, reason="structured report, "
                                "not a radiation dose report")
        root = tree(ds)
        proc = root.coded("121058", deep=False)
        is_ct = root.find("113811") is not None or root.find("113819", deep=False) is not None \
            or code_in(proc, {"P5-08000", "77477000"}, {"computed tomography x-ray"})
        return parse_ct(ds, root) if is_ct else parse_projection(ds, root)
    if sop_class in HEADER_SOP:
        return parse_header(ds)
    if sop_class in NM_SOP:
        return parse_nm(ds)
    if sop_class in CT_IMAGE_SOP:
        got = parse_ct_header(ds)
        if got is not None:
            return got
        study, _patient, _device = _header(ds)
        staff = {k: study[k] for k in ("study_uid", "operator_name", "performing_physician")}
        if staff["operator_name"] or staff["performing_physician"]:
            return ParsedObject("staff", sop_uid, sop_class, study=staff)
    if dosepage.wants_pixels(ds):
        got = parse_dose_page(ds)
        if got is not None:
            return got
    return ParsedObject("ignored", sop_uid, sop_class, reason="no dose information")


def parse_dose_page(ds) -> ParsedObject | None:
    """A scanner's dose screenshot (Secondary Capture) read by OCR: page 1 gives the weight,
    the table pages one event per acquisition with the scanner's own CTDIvol and DLP. Only
    trusted when the page names this very patient and day; None otherwise (ignored)."""
    page = dosepage.read(ds)
    if not page:
        return None
    if page.get("patient_id") != _s(ds.get("PatientID")) \
            or page.get("study_date") != str(ds.get("StudyDate", "")):
        return None
    for r in page["rows"]:  # DLP / CTDIvol is the scanned length in cm: a misread is not that
        if (r["dlp"] is None) != (r["ctdivol"] is None) or (
                r["dlp"] is not None and not 0.1 <= r["dlp"] / r["ctdivol"] <= 200):
            return None
    study, patient, device = _header(ds)
    study["modality"] = "CT"
    if page.get("weight_kg") and not study.get("weight_kg"):
        study["weight_kg"] = page["weight_kg"]
    events = []
    for r in page["rows"]:
        etype = dosepage.event_type(r["protocol"])
        events.append({
            "event_uid": dosepage.event_uid(study["study_uid"], r["section"], r["no"]),
            "event_datetime": study["study_datetime"], "event_type": etype,
            "protocol": r["protocol"], "comment": r["exam"], "ctdivol": r["ctdivol"],
            "dlp": r["dlp"], "kvp": r["kvp"], "ct_phantom_cm": r["phantom"],
            "is_localizer": etype == "localizer",
            "extra_json": {"source": "ocr", "row": r["no"], "section": r["section"]}})
    if not events and not study.get("weight_kg"):
        return None
    return ParsedObject("dose_page", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                        study=study, patient=patient, device=device, events=events)


# ------------------------------------------------------------------- NM / PET headers


def parse_nm(ds) -> ParsedObject:
    """Radiopharmaceutical Information Sequence (0054,0016): one event per administration.
    The event UID is derived from the administration (start, agent, activity), so every
    image and series of it lands on the same event."""
    study, patient, device = _header(ds)
    study["modality"] = study["modality"] or NM_SOP[str(ds.SOPClassUID)]
    events = []
    for rp in ds.get("RadiopharmaceuticalInformationSequence") or []:
        bq = _f(rp.get("RadionuclideTotalDose"))
        start = parse_dt(_s(rp.get("RadiopharmaceuticalStartDateTime"))) or _datetime(
            ds.get("StudyDate") or ds.get("SeriesDate"), rp.get("RadiopharmaceuticalStartTime"))
        nuclide = None
        if rp.get("RadionuclideCodeSequence"):
            nuclide = _s(rp.RadionuclideCodeSequence[0].get("CodeMeaning"), 64)
        agent = _s(rp.get("Radiopharmaceutical")) or (
            _s(rp.RadiopharmaceuticalCodeSequence[0].get("CodeMeaning"))
            if rp.get("RadiopharmaceuticalCodeSequence") else None)
        if not (bq or agent or nuclide):
            continue  # an empty sequence (some vendors fill only the radionuclide)
        key = f"{study['study_uid']}|{start}|{agent}|{bq}"
        events.append({
            "event_uid": "rp-" + hashlib.sha1(key.encode()).hexdigest()[:40],
            "event_datetime": start, "event_type": "radiopharmaceutical",
            "protocol": agent, "comment": nuclide,
            "extra_json": {"source": "header", "activity_mbq": bq / 1e6 if bq else None,
                           "half_life_s": _f(rp.get("RadionuclideHalfLife")),
                           "route": _s(rp.get("RadiopharmaceuticalRoute"), 64)},
        })
    if not events:
        return ParsedObject("ignored", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                            reason="NM/PET image without radiopharmaceutical information")
    return ParsedObject("nm_header", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                        study=study, patient=patient, device=device, events=events)


# ------------------------------------------------------------------------------- CT RDSR


def _phantom_cm(coded: tuple[str, str, str] | None) -> int | None:
    """The CTDI phantom diameter (16 head, 32 body) from its code: the DICOM IEC phantoms, or,
    for a vendor's own coding scheme, a meaning that names the size ("... Phantom, 32 cm")
    or the part ("head" / "body"). None when it says neither."""
    if not coded:
        return None
    if code_in(coded, {"113690"}, {"iec head dosimetry phantom"}):
        return 16
    if code_in(coded, {"113691"}, {"iec body dosimetry phantom"}):
        return 32
    meaning = (coded[2] or "").lower()
    m = re.search(r"\b(16|32)\s*cm\b", meaning)
    if m:
        return int(m.group(1))
    if re.search(r"\bhead\b", meaning) and not re.search(r"\bbody\b", meaning):
        return 16
    if re.search(r"\bbody\b", meaning) and not re.search(r"\bhead\b", meaning):
        return 32
    return None


def _scan_type(ds, itype: list[str], pitch: float | None) -> str | None:
    """localizer / spiral / axial of a CT image's series, from what the vendor sets: the
    standard Acquisition Type, else Scan Options (HELICAL, HELIX, HELICAL_CT, AXIAL, SCOUT,
    SURVIEW) or Siemens' fourth Image Type value (CT_SOM5 SPI / SEQ / TOP). The Image Type's
    "AXIAL" is the image plane, not the scan mode, so it is not read."""
    acq = str(ds.get("AcquisitionType", "") or "").upper()
    options = ds.get("ScanOptions")
    options = [] if options in (None, "") else [str(options)] if isinstance(options, str) \
        else [str(x) for x in options]
    options = [x.upper() for x in options]
    vendor = " ".join(itype[3:])  # e.g. "CT_SOM5 SPI"
    text = " ".join(options) + " " + vendor
    if "LOCALIZER" in itype or re.search(r"\b(SCOUT|SURVIEW|TOPO?)\b", text):
        return "localizer"
    if acq == "SPIRAL" or (not acq and pitch):
        return "spiral"
    if acq == "SEQUENCED" or re.search(r"\bDYNAMIC( MODE)?\b", text):  # GE: dynamic = sequential
        return "axial"
    if re.search(r"HELIX|HELICAL|SPIRAL|\bSPI\b", text):
        return "spiral"
    if re.search(r"\bAXIAL\b|\bSEQ(UENC\w*)?\b", text):
        return "axial"
    return None


def _ct_type(node: Node) -> str | None:
    c = node.coded("113820")
    for values, meanings, name in CT_TYPES:
        if code_in(c, values, meanings):
            return name
    return (c[2].lower() if c and c[2] else None)


def parse_ct(ds, root: Node) -> ParsedObject:
    study, patient, device = _header(ds)
    _observer(root, device)
    _staff(root, study)
    _patient_characteristics(root, study)
    study["modality"] = "CT"
    events = []
    for acq in root.find_all("113819"):
        etype = _ct_type(acq)
        phantom = acq.coded("113835")
        phantom_cm = _phantom_cm(phantom)
        # TID 10015: an alert was *raised* only when the report carries the forward estimate
        # or the reason for proceeding; "Alert Value Configured = Yes" alone is a setting.
        alert = acq.find("113900")
        alert_on = bool(alert and (alert.find("113905") or alert.find("113906")
                                   or alert.find("113907")))
        src = acq.find_all("113831", deep=True)
        kvs = [s.num("113733", "kV") for s in src if s.num("113733", "kV") is not None]
        mas = [s.num("113734", "mA") for s in src if s.num("113734", "mA") is not None]
        events.append({
            "event_uid": (acq.text("113769") or "")[:64] or None,
            "event_datetime": acq.datetime("111526") or root.datetime("113809", deep=False),
            "event_type": etype,
            "protocol": (acq.text("125203") or None),
            "anatomy": (acq.text("123014") or None),
            "comment": (acq.text("121106") or None),
            "ctdivol": acq.num("113830", "mGy"), "dlp": acq.num("113838", "mGy.cm"),
            "ct_phantom_cm": phantom_cm,
            "scan_length_mm": acq.num("113825", "mm"), "pitch": acq.num("113828"),
            "collimation_mm": acq.num("113827", "mm"),
            "rotation_time_s": acq.num("113834", "s"), "exposure_time_s": acq.num("113824", "s"),
            "kvp": sum(kvs) / len(kvs) if kvs else None,
            "ma": sum(mas) / len(mas) if mas else None,
            "ma_max": max((s.num("113833", "mA") or 0) for s in src) or None if src else None,
            "tcm": (acq.text("113842") or None),
            "ssde": acq.num("113930", "mGy"), "wed_mm": acq.num("113980", "mm"),
            "is_localizer": etype == "localizer",
            "dose_check_alert": alert_on,
            "extra_json": {"source": "rdsr"},
        })
    return ParsedObject("ct_rdsr", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                        study=study, patient=patient, device=device, events=events)


# ----------------------------------------------------------------------- projection RDSR


def _plane(c) -> str | None:
    if code_in(c, {"113621"}, {"plane b"}):
        return "B"
    if code_in(c, {"113620", "113622"}, {"plane a", "single plane"}):
        return "A"
    return None


def _filters(ev: Node) -> tuple[float | None, float | None]:
    cu = al = None
    for f in ev.find_all("113771", deep=True):
        mat = (f.text("113757") or "").lower()
        t = f.num("113758", "mm")
        if t is None:
            continue
        if "copper" in mat or mat == "cu":
            cu = (cu or 0) + t
        elif "alumin" in mat or mat == "al":
            al = (al or 0) + t
    return cu, al


def parse_projection(ds, root: Node) -> ParsedObject:
    study, patient, device = _header(ds)
    _observer(root, device)
    _staff(root, study)
    _patient_characteristics(root, study)
    events = []
    for ev in root.find_all("113706"):
        etc = ev.coded("113721")
        etype = "fluoro" if code_in(etc, *PROJ_FLUORO) else \
            "acquisition" if code_in(etc, *PROJ_ACQ) else (etc[2].lower() if etc else None)
        duration = ev.num("113742", "s")
        pulses = ev.find_all("113793", deep=True)
        widths = [p.number for p in pulses if p.number is not None]
        n_pulses = ev.num("113768")
        # Image Laterality (111027), or Laterality (SCT 272741003 / SRT G-C171) as a modifier
        # of the target region, as mammography reports write it
        lat = ev.coded("111027") or ev.coded("272741003") or ev.coded("G-C171")
        events.append({
            "event_uid": (ev.text("113769") or "")[:64] or None,
            "event_datetime": ev.datetime("111526"),
            "event_type": etype, "plane": _plane(ev.coded("113764")),
            "protocol": ev.text("125203"), "anatomy": ev.text("123014"),
            "comment": ev.text("121106"),
            "laterality": ({"right": "R", "left": "L", "both left and right": "B"}
                           .get((lat[2] or "").lower()) if lat else None),
            "view": ev.text("111031"),
            "dap": ev.num("122130", "Gy.cm2"), "rp_dose": ev.num("113738", "Gy"),
            "kvp": ev.num("113733", "kV"), "ma": ev.num("113734", "mA"),
            "mas": ev.num("113736", "mAs"),
            "exposure_time_s": ev.num("113824", "s"),
            "pulse_rate": ev.num("113791"), "n_pulses": int(n_pulses) if n_pulses else None,
            "pulse_width_ms": sum(widths) / len(widths) if widths else None,
            "fluoro_time_s": duration if etype == "fluoro" else None,
            "filter_cu_mm": _filters(ev)[0], "filter_al_mm": _filters(ev)[1],
            "grid": ev.text("111635"),
            # 113751 is the table longitudinal position, not a distance
            "sid_mm": ev.num("113750", "mm"),
            "dist_source_iso_mm": ev.num("113748", "mm"),
            "dist_source_rp_mm": ev.num("113737", "mm"),
            "table_long_mm": ev.num("113751", "mm"), "table_lat_mm": ev.num("113752", "mm"),
            "table_height_mm": ev.num("113753", "mm"),
            "patient_position": " ".join(x for x in (ev.text("113745"), ev.text("113743"),
                                                     ev.text("113744")) if x)[:64] or None,
            "field_w_mm": ev.num("113789", "mm"), "field_h_mm": ev.num("113788", "mm"),
            "primary_angle": ev.num("112011"), "secondary_angle": ev.num("112012"),
            "exposure_index": ev.num("113845"), "target_exposure_index": ev.num("113846"),
            "deviation_index": ev.num("113847"),
            "agd": ev.num("111631", "mGy"), "esd": ev.num("111636", "mGy"),
            "compression_thickness_mm": ev.num("111633", "mm"),
            "compression_force_n": ev.num("111647", "N"),
            "target_material": ev.text("111632"),
            "extra_json": {"source": "rdsr",
                           "duration_s": duration if etype != "fluoro" else None},
        })
    modality = _s(ds.get("Modality"), 16)
    if modality in (None, "SR", "OT"):
        modality = "RF" if any(e["event_type"] == "fluoro" for e in events) else "DX"
        if any(e["agd"] is not None for e in events):
            modality = "MG"
    study["modality"] = modality
    return ParsedObject("proj_rdsr", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                        study=study, patient=patient, device=device, events=events)


# --------------------------------------------------------------- CT image header (no SR)

PRIVATE_GROUP, PRIVATE_CREATOR = 0x0023, "DOSIMETRIX"  # added by the Q/R step: see below


def series_instances(ds) -> int | None:
    """Images in the CT series the object came from, when Q/R recorded it (private tag)."""
    try:
        v = ds.private_block(PRIVATE_GROUP, PRIVATE_CREATOR)[0x01].value
        return int(v) if v else None
    except (KeyError, ValueError, TypeError):
        return None


def mark_series_instances(ds, n: int) -> None:
    ds.private_block(PRIVATE_GROUP, PRIVATE_CREATOR, create=True).add_new(0x01, "IS", int(n))


def series_last_z(ds) -> float | None:
    """Z position (mm) of the series' last image, when Q/R recorded it (private tag)."""
    try:
        v = ds.private_block(PRIVATE_GROUP, PRIVATE_CREATOR)[0x02].value
        return float(v) if v not in (None, "") else None
    except (KeyError, ValueError, TypeError):
        return None


def mark_series_last_z(ds, z: float) -> None:
    ds.private_block(PRIVATE_GROUP, PRIVATE_CREATOR, create=True).add_new(0x02, "DS", f"{z:.4f}")


def _image_z(ds) -> float | None:
    pos = ds.get("ImagePositionPatient")
    try:
        return float(pos[2]) if pos is not None and len(pos) == 3 else None
    except (TypeError, ValueError):
        return None


# Helical scans irradiate more than the images show: the beam needs a margin of about one
# rotation's table travel before and after the reconstructed range. Fitted on one Toshiba
# Aquilion PRIME cardiac CTA against the console's own DLP (237 mm irradiated for 209 mm
# reconstructed with a 40 mm beam: 0.7 x collimation); to be refitted on more studies.
OVERRANGE_PER_COLLIMATION = 0.7


def parse_ct_header(ds) -> ParsedObject | None:
    """One CT image of a series, retrieved because the study has no dose report: its header
    carries CTDIvol, kV, mA, pitch and collimation of the whole (constant) series. The DLP
    is estimated as CTDIvol x scan length, the length being the series' image count (given
    by Q/R) times the slice spacing; the event says so (``dlp_estimated``). None when the
    image is not such a header (no CTDIvol, derived images): the staff names remain."""
    ctdivol = _f(ds.get("CTDIvol"))
    itype = [str(x).upper() for x in (ds.get("ImageType") or [])]
    if "DERIVED" in itype or "SECONDARY" in itype:
        return None
    if not ctdivol:
        return _parse_ct_technique(ds, itype)
    study, patient, device = _header(ds)
    study["modality"] = "CT"
    n = series_instances(ds)
    spacing = _f(ds.get("SpacingBetweenSlices")) or _f(ds.get("SliceThickness"))
    thickness = _f(ds.get("SliceThickness"))
    z0, z1 = _image_z(ds), series_last_z(ds)
    method, recon, over = "count_x_thickness", None, 0.0
    if z0 is not None and z1 is not None:
        # the true z range of the reconstruction: first to last image position plus one
        # slice (the series' image count x thickness is wrong when the images overlap)
        recon = abs(z1 - z0) + (abs(thickness) if thickness else 0.0)
        length, method = recon, "z_extent"
    else:
        length = n * abs(spacing) if n and spacing else None
    pitch = _f(ds.get("SpiralPitchFactor"))
    etype = _scan_type(ds, itype, pitch)
    seq = ds.get("CTDIPhantomTypeCodeSequence")
    phantom = None
    if seq:
        c = seq[0]
        phantom = _phantom_cm((str(c.get("CodeValue", "")), str(c.get("CodingSchemeDesignator", "")),
                               str(c.get("CodeMeaning", ""))))
    when = parse_dt(_s(ds.get("AcquisitionDateTime"))) or \
        _datetime(ds.get("AcquisitionDate"), ds.get("AcquisitionTime")) or study["study_datetime"]
    t = _f(ds.get("ExposureTime"))
    collimation = _f(ds.get("TotalCollimationWidth"))
    if method == "z_extent" and etype == "spiral" and collimation:
        over = OVERRANGE_PER_COLLIMATION * collimation
        length += over
    event = {
        "event_uid": _s(ds.get("SeriesInstanceUID"), 64), "event_datetime": when,
        "event_type": etype,
        "protocol": _s(ds.get("ProtocolName")) or _s(ds.get("SeriesDescription")),
        "anatomy": _s(ds.get("BodyPartExamined"), 128),
        "comment": _s(ds.get("SeriesDescription")),
        "ctdivol": ctdivol, "dlp": ctdivol * length / 10 if length else None,
        "ct_phantom_cm": phantom, "scan_length_mm": length, "pitch": pitch,
        "collimation_mm": collimation,
        "rotation_time_s": _f(ds.get("RevolutionTime")),
        "exposure_time_s": t / 1000 if t is not None else None,
        "kvp": _f(ds.get("KVP")), "ma": _f(ds.get("XRayTubeCurrent")),
        "is_localizer": etype == "localizer",
        "extra_json": {"source": "header", "dlp_estimated": length is not None,
                       "series_instances": n, "slice_spacing_mm": spacing,
                       "dlp_method": method, "recon_length_mm": recon,
                       "overrange_mm": over or None},
    }
    return ParsedObject("ct_header", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                        study=study, patient=patient, device=device, events=[event])


def _parse_ct_technique(ds, itype: list[str]) -> ParsedObject | None:
    """An original image of a legacy GE CT (ScanOptions "DYNAMIC MODE", "LOCALIZER MODE",
    ...): the header has no CTDIvol, DLP, pitch or collimation, only the technique. One event
    per series with kVp, mA and exposure time and no dose, so the exam is kept for history
    (the dose cannot be invented; it would need the scanner's measured CTDI per mAs)."""
    options = str(ds.get("ScanOptions") or "").upper()
    if "ORIGINAL" not in itype or "GE" not in str(ds.get("Manufacturer", "")).upper() \
            or not re.search(r"\b\w+ MODE\b", options):
        return None
    kvp, ma, t = _f(ds.get("KVP")), _f(ds.get("XRayTubeCurrent")), _f(ds.get("ExposureTime"))
    if not kvp or not (ma or t):
        return None
    study, patient, device = _header(ds)
    study["modality"] = "CT"
    etype = _scan_type(ds, itype, None)
    when = _datetime(ds.get("AcquisitionDate"), ds.get("AcquisitionTime")) or study["study_datetime"]
    event = {
        "event_uid": _s(ds.get("SeriesInstanceUID"), 64), "event_datetime": when,
        "event_type": etype, "protocol": _s(ds.get("ProtocolName")) or _s(ds.get("SeriesDescription")),
        "anatomy": _s(ds.get("BodyPartExamined"), 128), "comment": _s(ds.get("SeriesDescription")),
        "kvp": kvp, "ma": ma, "exposure_time_s": t / 1000 if t else None,
        "mas": ma * t / 1000 if ma and t else None, "is_localizer": etype == "localizer",
        "extra_json": {"source": "header", "dose_missing": True, "legacy_technique": True,
                       "series_instances": series_instances(ds),
                       "slice_thickness_mm": _f(ds.get("SliceThickness")),
                       "scan_options": options},
    }
    return ParsedObject("ct_header", str(ds.SOPInstanceUID), str(ds.SOPClassUID),
                        study=study, patient=patient, device=device, events=[event])


# ------------------------------------------------------------------------- image headers

EI_UNITS = [("fuji", "S"), ("carestream", "EI (mbels)"), ("kodak", "EI (mbels)"),
            ("agfa", "lgM (bels)"), ("konica", "S"), ("canon", "REX"), ("swissray", "DI"),
            ("philips", "EI"), ("siemens", "EXI (µGy)")]


def parse_header(ds) -> ParsedObject:
    study, patient, device = _header(ds)
    sop_class = str(ds.SOPClassUID)
    study["modality"] = study["modality"] or HEADER_SOP[sop_class]
    man = (device["manufacturer"] or "").lower()
    model = (device["model_name"] or "").lower()

    protocol = _s(ds.get("ProtocolName"))
    if not protocol and man == "toshiba_mec":
        protocol = _s(ds.get("ImageComments"))
    if not protocol:
        protocol = _s(ds.get("SeriesDescription"))
    if not protocol and ds.get("PerformedProtocolCodeSequence"):
        protocol = _s(ds.PerformedProtocolCodeSequence[0].get("CodeMeaning"))
    if not protocol:
        protocol = _s(ds.get("AcquisitionDeviceProcessingDescription"))

    when = parse_dt(_s(ds.get("AcquisitionDateTime"))) or \
        _datetime(ds.get("AcquisitionDate"), ds.get("AcquisitionTime")) or \
        _datetime(ds.get("ContentDate"), ds.get("ContentTime")) or study["study_datetime"]

    ma = _f(ds.get("XRayTubeCurrentInuA"))
    ma = ma / 1000 if ma is not None else _f(ds.get("XRayTubeCurrent"))
    t = _f(ds.get("ExposureTimeInuS"))
    t = t / 1e6 if t is not None else (_f(ds.get("ExposureTime")) / 1000
                                       if _f(ds.get("ExposureTime")) is not None else None)
    mas = _f(ds.get("ExposureInuAs"))
    mas = mas / 1000 if mas is not None else _f(ds.get("Exposure"))
    dap = _f(ds.get("ImageAndFluoroscopyAreaDoseProduct"))  # dGy·cm² by definition
    sid = _f(ds.get("DistanceSourceToDetector"))
    if sid and "kodak" in man and "dr 7500" in model:
        sid *= 100  # reports decimetres
    cu = al = None
    mats = ds.get("FilterMaterial")
    thick = ds.get("FilterThicknessMinimum")
    if mats is not None and thick is not None:
        mats = list(mats) if mats.__class__.__name__ == "MultiValue" else [mats]
        thick = list(thick) if thick.__class__.__name__ == "MultiValue" else [thick]
        for m, th in zip(mats, thick, strict=False):
            m, th = str(m).upper(), _f(th)
            if th is None:
                continue
            if m in ("COPPER", "CU"):
                cu = (cu or 0) + th
            elif m in ("ALUMINUM", "ALUMINIUM", "AL"):
                al = (al or 0) + th
    ei_unit = next((u for k, u in EI_UNITS if k in man), None)
    event = {
        "event_uid": str(ds.SOPInstanceUID), "event_datetime": when,
        "event_type": "acquisition", "protocol": protocol,
        "anatomy": _s(ds.get("BodyPartExamined"), 128), "view": _s(ds.get("ViewPosition"), 64),
        "laterality": _s(ds.get("ImageLaterality") or ds.get("Laterality"), 2),
        "comment": _s(ds.get("SeriesDescription")),
        "kvp": _f(ds.get("KVP")), "ma": ma, "mas": mas, "exposure_time_s": t,
        "dap": dap / 10 if dap is not None else None,
        "esd": _f(ds.get("EntranceDoseInmGy")),
        "exposure_index": _f(ds.get("ExposureIndex")),
        "target_exposure_index": _f(ds.get("TargetExposureIndex")),
        "deviation_index": _f(ds.get("DeviationIndex")),
        "ei_vendor_value": _f(ds.get("RelativeXRayExposure")),
        "ei_vendor_unit": ei_unit if ds.get("RelativeXRayExposure") is not None else None,
        "sid_mm": sid, "ssd_mm": _f(ds.get("DistanceSourceToPatient")),
        "filter_cu_mm": cu, "filter_al_mm": al, "grid": _s(ds.get("Grid"), 64),
        # Organ Dose (0040,0316) is in dGy: the mean glandular dose in mGy is 100 x it
        "agd": _dgy_to_mgy(ds.get("OrganDose")) if study["modality"] == "MG" else None,
        "compression_thickness_mm": _f(ds.get("BodyPartThickness")),
        "compression_force_n": _f(ds.get("CompressionForce")),
        "target_material": _s(ds.get("AnodeTargetMaterial"), 32),
        "extra_json": {"source": "header",
                       "for_processing": sop_class in FOR_PROCESSING},
    }
    return ParsedObject("header", str(ds.SOPInstanceUID), sop_class,
                        study=study, patient=patient, device=device, events=[event])


def age_from(study: dict, patient: dict) -> tuple[int | None, str]:
    """(age_days, precision) from birth date and study date, else DICOM PatientAge."""
    b, d = patient.get("birth_date"), study.get("study_datetime")
    if b and d and d.date() >= b:
        return (d.date() - b).days, "exact"
    parsed = parse_dicom_age(study.get("dicom_age"))
    if parsed:
        amount, unit = parsed
        return int(amount * {"D": 1, "W": 7, "M": 30.4375, "Y": 365.25}[unit]), "dicom_age"
    return None, "unknown"
