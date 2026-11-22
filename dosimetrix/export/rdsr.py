"""X-Ray Radiation Dose SR (IHE REM) written from the stored data of a study.

* CT studies → TID 10011 (CT Radiation Dose), one CT Acquisition per stored series;
* everything else (DX/CR/RF/XA/MG) → TID 10001 (Projection X-Ray Radiation Dose), one
  Irradiation Event X-Ray Data container per stored exposure or fluoroscopy run.

The objects are DICOM Part 10 files (Explicit VR Little Endian) that our own parser, and
other dose monitors, read back. Values are what Dosimetrix stored, in DICOM's units; nothing
is recomputed. A weight is written only when it was reported (estimated weights are not).

Pseudonymised by default: patient ID → the keyed pseudonym, no name, birth date, accession or
physicians; study / series / SOP UIDs replaced by stable derived UIDs (the same study
always gives the same UIDs, so a receiver can de-duplicate). Kept: exact study date and
time, age, sex, study description, protocol names and comments — personal data still.
``identified=True`` keeps the originals (needs the pid role in the API).

This module only builds objects and writes files. Sending them (C-STORE to a PACS or a
registry) is deliberately not part of it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from .. import __version__
from ..db.models import Event, SizeSource, Study

RDSR_SOP = "1.2.840.10008.5.1.4.1.1.88.67"
ESTIMATED = {SizeSource.estimated_reference, SizeSource.estimated_visual}

CT_TYPE = {
    "localizer": ("113805", "DCM", "Constant Angle Acquisition"),
    "axial": ("113804", "DCM", "Sequenced Acquisition"),
    "spiral": ("116152004", "SCT", "Spiral Acquisition"),
    "stationary": ("113806", "DCM", "Stationary Acquisition"),
    "free": ("113807", "DCM", "Free Acquisition"),
    "cone_beam": ("702569007", "SCT", "Cone beam acquisition"),
}
PHANTOM = {16: ("113690", "DCM", "IEC Head Dosimetry Phantom"),
           32: ("113691", "DCM", "IEC Body Dosimetry Phantom")}
PLANE = {"A": ("113620", "DCM", "Plane A"), "B": ("113621", "DCM", "Plane B"),
         None: ("113622", "DCM", "Single Plane")}
LATERALITY = {"L": ("7771000", "SCT", "Left"), "R": ("24028007", "SCT", "Right"),
              "B": ("51440002", "SCT", "Both left and right")}
FLUORO = ("44491008", "SCT", "Fluoroscopy")
STATIONARY = ("113611", "DCM", "Stationary Acquisition")
LOCAL = "99DOSIMETRIX"  # free-text values we hold only as text (target region, view)


# --------------------------------------------------------------------------- builders


def _code(value: str, scheme: str, meaning: str) -> Dataset:
    c = Dataset()
    c.CodeValue, c.CodingSchemeDesignator, c.CodeMeaning = value, scheme, meaning[:64]
    return c


def _local(text: str) -> tuple[str, str, str]:
    key = hashlib.sha1(text.encode()).hexdigest()[:12].upper()
    return key, LOCAL, text


def _item(vt: str, concept: tuple[str, str, str], value=None, unit: str | None = None,
          children: list | None = None, rel: str = "CONTAINS") -> Dataset:
    it = Dataset()
    it.RelationshipType = rel
    it.ValueType = vt
    it.ConceptNameCodeSequence = Sequence([_code(*concept)])
    if vt == "NUM":
        mv = Dataset()
        mv.NumericValue = _ds_number(value)
        mv.MeasurementUnitsCodeSequence = Sequence([_code(unit or "1", "UCUM", unit or "1")])
        it.MeasuredValueSequence = Sequence([mv])
    elif vt == "CODE":
        it.ConceptCodeSequence = Sequence([_code(*value)])
    elif vt == "TEXT":
        it.TextValue = str(value)[:1024]
    elif vt == "UIDREF":
        it.UID = value
    elif vt == "DATETIME":
        it.DateTime = value.strftime("%Y%m%d%H%M%S")
    elif vt == "CONTAINER":
        it.ContinuityOfContent = "SEPARATE"
    if children:
        it.ContentSequence = Sequence(children)
    return it


def _ds_number(v: float) -> str:
    """Decimal String: at most 16 characters."""
    s = repr(float(v))
    if len(s) <= 16:
        return s.removesuffix(".0") if s.endswith(".0") else s
    return f"{float(v):.10g}"[:16]


def _num(out: list, concept, value, unit) -> None:
    if value is not None:
        out.append(_item("NUM", concept, value, unit))


def _text(out: list, concept, value) -> None:
    if value:
        out.append(_item("TEXT", concept, value))


def _uid(secret: bytes, *parts: str) -> str:
    return generate_uid(entropy_srcs=[secret.hex(), "dosimetrix-rdsr", *parts])


def _event_uid(secret: bytes, study_uid: str, ev: Event, identified: bool) -> str:
    """The stored UID when identified (and valid); else derived: stored event UIDs can be
    image SOP Instance UIDs or share the study's root, i.e. link back to the PACS."""
    if identified and ev.event_uid and _is_uid(ev.event_uid):
        return ev.event_uid
    return _uid(secret, study_uid, "event", ev.event_uid or str(ev.id))


def _is_uid(v: str) -> bool:
    return len(v) <= 64 and all(p.isdigit() and (p == "0" or not p.startswith("0"))
                                for p in v.split("."))


# ----------------------------------------------------------------------------- content


def _observer(study: Study) -> list[Dataset]:
    d = study.device
    out = [_item("CODE", ("121005", "DCM", "Observer Type"), ("121007", "DCM", "Device"),
                 rel="HAS OBS CONTEXT")]
    uid = d.device_observer_uid if d and d.device_observer_uid else None
    if uid is None:
        key = (d.identity_key if d else f"study:{study.id}")
        uid = generate_uid(entropy_srcs=["dosimetrix-device", key])
    out.append(_item("UIDREF", ("121012", "DCM", "Device Observer UID"), uid,
                     rel="HAS OBS CONTEXT"))
    for code, meaning, attr in (("121013", "Device Observer Name", "station_name"),
                                ("121014", "Device Observer Manufacturer", "manufacturer"),
                                ("121015", "Device Observer Model Name", "model_name"),
                                ("121016", "Device Observer Serial Number", "serial_number")):
        v = getattr(d, attr, None) if d else None
        if v:
            out.append(_item("TEXT", (code, "DCM", meaning), v, rel="HAS OBS CONTEXT"))
    return out


def _patient_characteristics(study: Study) -> list[Dataset]:
    ch: list[Dataset] = []
    if study.weight_kg and study.weight_source not in ESTIMATED:
        _num(ch, ("29463-7", "LN", "Patient's Weight"), study.weight_kg, "kg")
    if study.height_m and study.height_source not in ESTIMATED:
        _num(ch, ("8302-2", "LN", "Patient Height"), study.height_m, "m")
    if not ch:
        return []
    return [_item("CONTAINER", ("121118", "DCM", "Patient Characteristics"), children=ch)]


def _ct_content(study: Study, events: list[Event], secret: bytes,
                identified: bool) -> list[Dataset]:
    acqs = []
    for ev in events:
        params: list[Dataset] = []
        _num(params, ("113824", "DCM", "Exposure Time"), ev.exposure_time_s, "s")
        _num(params, ("113825", "DCM", "Scanning Length"), ev.scan_length_mm, "mm")
        _num(params, ("113827", "DCM", "Nominal Total Collimation Width"),
             ev.collimation_mm, "mm")
        _num(params, ("113828", "DCM", "Pitch Factor"), ev.pitch, "{ratio}")
        src: list[Dataset] = []
        _num(src, ("113733", "DCM", "KVP"), ev.kvp, "kV")
        _num(src, ("113833", "DCM", "Maximum X-Ray Tube Current"), ev.ma_max, "mA")
        _num(src, ("113734", "DCM", "X-Ray Tube Current"), ev.ma, "mA")
        _num(src, ("113834", "DCM", "Exposure Time per Rotation"), ev.rotation_time_s, "s")
        if src:
            params.append(_item("CONTAINER", ("113831", "DCM", "CT X-Ray Source Parameters"),
                                children=src))
        dose: list[Dataset] = []
        _num(dose, ("113830", "DCM", "Mean CTDIvol"), ev.ctdivol, "mGy")
        if ev.ct_phantom_cm in PHANTOM:
            dose.append(_item("CODE", ("113835", "DCM", "CTDIw Phantom Type"),
                              PHANTOM[ev.ct_phantom_cm]))
        _num(dose, ("113838", "DCM", "DLP"), ev.dlp, "mGy.cm")
        _num(dose, ("113930", "DCM", "Size Specific Dose Estimation"), ev.ssde, "mGy")
        _num(dose, ("113980", "DCM", "Water Equivalent Diameter"), ev.wed_mm, "mm")
        ch: list[Dataset] = []
        _text(ch, ("125203", "DCM", "Acquisition Protocol"), ev.protocol)
        if ev.anatomy:
            ch.append(_item("CODE", ("123014", "DCM", "Target Region"), _local(ev.anatomy)))
        etype = "localizer" if ev.is_localizer else ev.event_type
        if etype in CT_TYPE:
            ch.append(_item("CODE", ("113820", "DCM", "CT Acquisition Type"), CT_TYPE[etype]))
        ch.append(_item("UIDREF", ("113769", "DCM", "Irradiation Event UID"),
                        _event_uid(secret, study.study_uid, ev, identified)))
        if ev.event_datetime:
            ch.append(_item("DATETIME", ("111526", "DCM", "DateTime Started"),
                            ev.event_datetime))
        _text(ch, ("121106", "DCM", "Comment"), ev.comment)
        if ev.tcm:
            _text(ch, ("113842", "DCM", "X-Ray Modulation Type"), ev.tcm)
        if params:
            ch.append(_item("CONTAINER", ("113822", "DCM", "CT Acquisition Parameters"),
                            children=params))
        if dose:
            ch.append(_item("CONTAINER", ("113829", "DCM", "CT Dose"), children=dose))
        acqs.append(_item("CONTAINER", ("113819", "DCM", "CT Acquisition"), children=ch))
    acc: list[Dataset] = []
    _num(acc, ("113812", "DCM", "Total Number of Irradiation Events"), len(events),
         "{events}")
    _num(acc, ("113813", "DCM", "CT Dose Length Product Total"), study.total_dlp, "mGy.cm")
    return [_item("CONTAINER", ("113811", "DCM", "CT Accumulated Dose Data"), children=acc),
            *acqs]


def _proj_content(study: Study, events: list[Event], secret: bytes,
                  identified: bool) -> list[Dataset]:
    out = []
    for plane in sorted({e.plane for e in events}, key=lambda p: p or ""):
        evs = [e for e in events if e.plane == plane]
        fl = [e for e in evs if e.event_type == "fluoro"]
        ac = [e for e in evs if e.event_type != "fluoro"]

        def total(xs, attr):
            v = [getattr(e, attr) for e in xs if getattr(e, attr) is not None]
            return sum(v) if v else None
        tot = [_item("CODE", ("113764", "DCM", "Acquisition Plane"),
                     PLANE.get(plane, PLANE[None]))]
        _num(tot, ("113722", "DCM", "Dose Area Product Total"), total(evs, "dap"), "Gy.cm2")
        _num(tot, ("113726", "DCM", "Fluoro Dose Area Product Total"), total(fl, "dap"),
             "Gy.cm2")
        _num(tot, ("113727", "DCM", "Acquisition Dose Area Product Total"),
             total(ac, "dap"), "Gy.cm2")
        _num(tot, ("113725", "DCM", "Dose (RP) Total"), total(evs, "rp_dose"), "Gy")
        _num(tot, ("113730", "DCM", "Total Fluoro Time"), total(fl, "fluoro_time_s"), "s")
        _num(tot, ("113731", "DCM", "Total Number of Radiographic Frames"),
             len(ac) or None, "1")
        out.append(_item("CONTAINER", ("113702", "DCM", "Accumulated X-Ray Dose Data"),
                         children=tot))
    for ev in events:
        ch = [_item("CODE", ("113764", "DCM", "Acquisition Plane"),
                    PLANE.get(ev.plane, PLANE[None]))]
        if ev.event_datetime:
            ch.append(_item("DATETIME", ("111526", "DCM", "DateTime Started"),
                            ev.event_datetime))
        ch.append(_item("CODE", ("113721", "DCM", "Irradiation Event Type"),
                        FLUORO if ev.event_type == "fluoro" else STATIONARY))
        _text(ch, ("125203", "DCM", "Acquisition Protocol"), ev.protocol)
        if ev.anatomy:
            ch.append(_item("CODE", ("123014", "DCM", "Target Region"), _local(ev.anatomy)))
        if ev.laterality in LATERALITY:
            ch.append(_item("CODE", ("111027", "DCM", "Image Laterality"),
                            LATERALITY[ev.laterality]))
        if ev.view:
            ch.append(_item("CODE", ("111031", "DCM", "Image View"), _local(ev.view)))
        ch.append(_item("UIDREF", ("113769", "DCM", "Irradiation Event UID"),
                        _event_uid(secret, study.study_uid, ev, identified)))
        _text(ch, ("121106", "DCM", "Comment"), ev.comment)
        for concept, attr, unit in (
                (("122130", "DCM", "Dose Area Product"), "dap", "Gy.cm2"),
                (("113738", "DCM", "Dose (RP)"), "rp_dose", "Gy"),
                (("113750", "DCM", "Distance Source to Detector"), "sid_mm", "mm"),
                (("113748", "DCM", "Distance Source to Isocenter"), "dist_source_iso_mm", "mm"),
                (("113737", "DCM", "Distance Source to Reference Point"),
                 "dist_source_rp_mm", "mm"),
                (("113751", "DCM", "Table Longitudinal Position"), "table_long_mm", "mm"),
                (("113752", "DCM", "Table Lateral Position"), "table_lat_mm", "mm"),
                (("113753", "DCM", "Table Height Position"), "table_height_mm", "mm"),
                (("113789", "DCM", "Collimated Field Width"), "field_w_mm", "mm"),
                (("113788", "DCM", "Collimated Field Height"), "field_h_mm", "mm"),
                (("112011", "DCM", "Positioner Primary Angle"), "primary_angle", "deg"),
                (("112012", "DCM", "Positioner Secondary Angle"), "secondary_angle", "deg"),
                (("113845", "DCM", "Exposure Index"), "exposure_index", "1"),
                (("113846", "DCM", "Target Exposure Index"), "target_exposure_index", "1"),
                (("113847", "DCM", "Deviation Index"), "deviation_index", "1"),
                (("111631", "DCM", "Average Glandular Dose"), "agd", "mGy"),
                (("111636", "DCM", "Entrance Exposure at RP"), "esd", "mGy"),
                (("111633", "DCM", "Compression Thickness"), "compression_thickness_mm", "mm"),
                (("111647", "DCM", "Compression Force"), "compression_force_n", "N")):
            _num(ch, concept, getattr(ev, attr), unit)
        src: list[Dataset] = []
        _num(src, ("113733", "DCM", "KVP"), ev.kvp, "kV")
        _num(src, ("113734", "DCM", "X-Ray Tube Current"), ev.ma, "mA")
        _num(src, ("113736", "DCM", "Exposure"), ev.mas, "mA.s")
        _num(src, ("113824", "DCM", "Exposure Time"), ev.exposure_time_s, "s")
        _num(src, ("113791", "DCM", "Pulse Rate"), ev.pulse_rate, "{pulse}/s")
        _num(src, ("113768", "DCM", "Number of Pulses"), ev.n_pulses, "1")
        if ev.event_type == "fluoro":
            _num(src, ("113742", "DCM", "Irradiation Duration"), ev.fluoro_time_s, "s")
        if ev.pulse_width_ms is not None:
            src.append(_item("NUM", ("113793", "DCM", "Pulse Width"), ev.pulse_width_ms,
                             "ms"))
        _text(src, ("111632", "DCM", "Anode Target Material"), ev.target_material)
        _text(src, ("111635", "DCM", "X-Ray Grid"), ev.grid)
        filters = [(m, t) for m, t in (("Copper", ev.filter_cu_mm),
                                       ("Aluminum", ev.filter_al_mm)) if t]
        for mat, t in filters:
            src.append(_item("CONTAINER", ("113771", "DCM", "X-Ray Filters"), children=[
                _item("TEXT", ("113757", "DCM", "X-Ray Filter Material"), mat),
                _item("NUM", ("113758", "DCM", "X-Ray Filter Thickness Minimum"), t, "mm")]))
        ch.extend(src)
        out.append(_item("CONTAINER", ("113706", "DCM", "Irradiation Event X-Ray Data"),
                         children=ch))
    return out


# ------------------------------------------------------------------------------ public


def build(study: Study, secret: bytes, *, identified: bool = False,
          now: dt.datetime | None = None) -> Dataset:
    """The dose SR of one stored study (a pydicom FileDataset-like Dataset with file meta)."""
    now = now or dt.datetime.now()
    events = sorted(study.events, key=lambda e: (e.event_datetime or dt.datetime.min, e.id))
    is_ct = (study.modality or "").upper() == "CT"
    pat = study.patient
    d = study.device

    ds = Dataset()
    ds.SOPClassUID = RDSR_SOP
    ds.SOPInstanceUID = _uid(secret, study.study_uid, "sop", "id" if identified else "anon")
    if identified:
        ds.StudyInstanceUID = study.study_uid
        ds.AccessionNumber = "" if study.accession_is_hashed else (study.accession or "")
        ds.PatientID = (pat.patient_id if pat else "") or ""
        ds.PatientName = (pat.name if pat else "") or ""
        ds.PatientBirthDate = pat.birth_date.strftime("%Y%m%d") if pat and pat.birth_date \
            else ""
        ds.ReferringPhysicianName = study.referring_physician or ""
    else:
        ds.StudyInstanceUID = _uid(secret, study.study_uid, "study")
        ds.AccessionNumber = ""
        ds.PatientID = (pat.pseudo_id if pat else None) or f"S-{study.id}"
        ds.PatientName = ""
        ds.PatientBirthDate = ""
        ds.ReferringPhysicianName = ""
        ds.PatientIdentityRemoved = "YES"
        ds.DeidentificationMethod = "Dosimetrix pseudonym; derived UIDs"
    ds.SeriesInstanceUID = _uid(secret, ds.StudyInstanceUID, "series")
    ds.PatientSex = (pat.sex if pat and pat.sex in ("M", "F", "O") else "") or ""
    if study.age_days is not None:
        ds.PatientAge = _dicom_age(study.age_days)
    if study.weight_kg and study.weight_source not in ESTIMATED:
        ds.PatientWeight = _ds_number(study.weight_kg)
    if study.height_m and study.height_source not in ESTIMATED:
        ds.PatientSize = _ds_number(study.height_m)
    when = study.study_datetime or now
    ds.StudyDate, ds.StudyTime = when.strftime("%Y%m%d"), when.strftime("%H%M%S")
    ds.ContentDate, ds.ContentTime = now.strftime("%Y%m%d"), now.strftime("%H%M%S")
    ds.StudyID = ""
    ds.StudyDescription = study.study_description or ""
    ds.Modality = "SR"
    ds.SeriesNumber = 999
    ds.InstanceNumber = 1
    ds.SeriesDescription = "Radiation Dose Information (Dosimetrix)"
    ds.Manufacturer = (d.manufacturer if d else "") or ""
    ds.ManufacturerModelName = (d.model_name if d else "") or ""
    ds.DeviceSerialNumber = (d.serial_number if d else "") or ""
    ds.StationName = (d.station_name if d else "") or ""
    ds.InstitutionName = (d.institution_name if d else "") or ""
    ds.SoftwareVersions = f"Dosimetrix {__version__}"
    ds.ReferencedPerformedProcedureStepSequence = Sequence([])
    ds.PerformedProcedureCodeSequence = Sequence([])
    ds.CompletionFlag, ds.VerificationFlag = "COMPLETE", "UNVERIFIED"
    ds.ValueType = "CONTAINER"
    ds.ContinuityOfContent = "SEPARATE"
    ds.ConceptNameCodeSequence = Sequence([_code("113701", "DCM",
                                                 "X-Ray Radiation Dose Report")])
    tpl = Dataset()
    tpl.MappingResource, tpl.TemplateIdentifier = "DCMR", "10011" if is_ct else "10001"
    ds.ContentTemplateSequence = Sequence([tpl])
    proc = ("77477000", "SCT", "Computed Tomography X-Ray") if is_ct else \
        ("113704", "DCM", "Projection X-Ray")
    content = [_item("CODE", ("121058", "DCM", "Procedure reported"), proc,
                     rel="HAS CONCEPT MOD"),
               *_observer(study)]
    start = events[0].event_datetime if events and events[0].event_datetime else \
        study.study_datetime
    if start:
        content.append(_item("DATETIME", ("113809", "DCM", "Start of X-Ray Irradiation"),
                             start))
    content += _patient_characteristics(study)
    content += _ct_content(study, events, secret, identified) if is_ct else \
        _proj_content(study, events, secret, identified)
    content.append(_item("CODE", ("113854", "DCM", "Source of Dose Information"),
                         ("113856", "DCM", "Automated Data Collection")))
    ds.ContentSequence = Sequence(content)

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = RDSR_SOP
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = generate_uid(entropy_srcs=["dosimetrix", __version__])
    meta.ImplementationVersionName = f"DOSIMETRIX_{__version__}"[:16]
    ds.file_meta = meta
    ds.SpecificCharacterSet = "ISO_IR 192"
    return ds


def _dicom_age(days: int) -> str:
    if days < 31:
        return f"{max(days, 0):03d}D"
    if days < 2 * 365:
        return f"{min(int(days / 30.4375), 999):03d}M"
    return f"{min(int(days / 365.25), 999):03d}Y"


def write(ds: Dataset, target) -> None:
    """Write a Part 10 file to a path or binary file object."""
    ds.save_as(target, enforce_file_format=True)


def write_studies(session, study_ids: list[int], out_dir: str | Path, secret: bytes, *,
                  identified: bool = False) -> list[Path]:
    """One file per study into ``out_dir`` (named by SOP Instance UID). Local files only."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for sid in study_ids:
        st = session.get(Study, sid)
        if st is None or not st.events:
            continue
        ds = build(st, secret, identified=identified)
        p = out / f"{ds.SOPInstanceUID}.dcm"
        write(ds, p)
        paths.append(p)
    return paths
