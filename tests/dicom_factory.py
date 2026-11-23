"""Synthetic DICOM objects for tests (no real data). Built with pydicom only."""

from __future__ import annotations

from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

RDSR = "1.2.840.10008.5.1.4.1.1.88.67"
SC = "1.2.840.10008.5.1.4.1.1.7"
DX_PRES = "1.2.840.10008.5.1.4.1.1.1.1"
DX_PROC = "1.2.840.10008.5.1.4.1.1.1.1.1"
TEXT_SR = "1.2.840.10008.5.1.4.1.1.88.11"


def code(value, meaning, scheme="DCM"):
    c = Dataset()
    c.CodeValue, c.CodingSchemeDesignator, c.CodeMeaning = value, scheme, meaning
    return c


def item(vt, concept, value=None, unit=None, children=None, rel="CONTAINS"):
    it = Dataset()
    it.RelationshipType = rel
    it.ValueType = vt
    it.ConceptNameCodeSequence = Sequence([code(*concept)])
    if vt == "NUM":
        mv = Dataset()
        mv.NumericValue = str(value)
        mv.MeasurementUnitsCodeSequence = Sequence([code(unit, unit, "UCUM")])
        it.MeasuredValueSequence = Sequence([mv])
    elif vt == "CODE":
        it.ConceptCodeSequence = Sequence([code(*value)])
    elif vt == "TEXT":
        it.TextValue = value
    elif vt == "UIDREF":
        it.UID = value
    elif vt == "DATETIME":
        it.DateTime = value
    elif vt == "PNAME":
        it.PersonName = value
    elif vt == "CONTAINER":
        it.ContinuityOfContent = "SEPARATE"
    if children:
        it.ContentSequence = Sequence(children)
    return it


def header(ds, *, study_uid, sop_uid, modality, patient_id="P1", birth="20180101",
           study_date="20251106", manufacturer="ACME", model="Model X", serial="SN1",
           station="ROOM1", weight=None):
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = sop_uid + ".9"
    ds.Modality = modality
    ds.PatientID = patient_id
    ds.PatientName = "TEST^PATIENT"
    ds.PatientBirthDate = birth
    ds.PatientSex = "F"
    ds.StudyDate, ds.StudyTime = study_date, "100000"
    ds.AccessionNumber = "ACC" + study_uid[-3:]
    ds.StudyDescription = "Test study"
    ds.Manufacturer, ds.ManufacturerModelName = manufacturer, model
    ds.DeviceSerialNumber, ds.StationName = serial, station
    if weight is not None:
        ds.PatientWeight = str(weight)
    return ds


def ct_rdsr(study_uid="1.2.3.100", sop_uid="1.2.3.100.1", events=None, sop_class=RDSR,
            dlp_unit="mGy.cm", alert_raised=False, observer_uid="9.9.9", staff=()):
    """``staff``: (name, role code) person participants, in the first acquisition."""
    ds = header(Dataset(), study_uid=study_uid, sop_uid=sop_uid,
                modality="SR" if sop_class == RDSR else "CT")
    ds.SOPClassUID = sop_class
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = Sequence([code("113701", "X-Ray Radiation Dose Report")])
    tpl = Dataset()
    tpl.TemplateIdentifier, tpl.MappingResource = "10011", "DCMR"
    ds.ContentTemplateSequence = Sequence([tpl])
    events = events or [
        ("e1", "113805", "Constant Angle Acquisition", "Topogram", 0.1, 2.5),
        ("e2", "P5-08001", "Spiral Acquisition", "Head routine", 45.0, 820.0),
    ]
    acqs = []
    people = [item("PNAME", ("113870", "Person Name"), name, children=[
        item("CODE", ("113875", "Person Role in Procedure"), (role, role))])
        for name, role in staff]
    for uid, tcode, tmean, proto, ctdi, dlp in events:
        dose_children = [
            item("NUM", ("113830", "Mean CTDIvol"), ctdi, "mGy"),
            item("CODE", ("113835", "CTDIw Phantom Type"),
                 ("113690", "IEC Head Dosimetry Phantom")),
            item("NUM", ("113838", "DLP"), dlp, dlp_unit),
        ]
        if alert_raised:
            dose_children.append(item("CONTAINER", ("113900", "Dose Check Alert Details"),
                                      children=[item("TEXT", ("113907", "Reason for "
                                                              "proceeding"), "clinical")]))
        acqs.append(item("CONTAINER", ("113819", "CT Acquisition"), children=[
            item("TEXT", ("125203", "Acquisition Protocol"), proto),
            item("CODE", ("123014", "Target Region"), ("T-D1100", "Head", "SRT")),
            item("CODE", ("113820", "CT Acquisition Type"), (tcode, tmean)),
            item("UIDREF", ("113769", "Irradiation Event UID"), uid),
            item("CONTAINER", ("113822", "CT Acquisition Parameters"), children=[
                item("NUM", ("113824", "Exposure Time"), 3.0, "s"),
                item("NUM", ("113825", "Scanning Length"), 18.0, "cm"),
                item("NUM", ("113828", "Pitch Factor"), 0.9, "{ratio}"),
                item("CONTAINER", ("113831", "CT X-Ray Source Parameters"), children=[
                    item("NUM", ("113733", "KVP"), 120, "kV"),
                    item("NUM", ("113734", "X-Ray Tube Current"), 200, "mA"),
                ]),
            ]),
            item("CONTAINER", ("113829", "CT Dose"), children=dose_children),
            *(people if not acqs else []),
        ]))
    ds.ContentSequence = Sequence([
        item("CODE", ("121058", "Procedure reported"),
             ("P5-08000", "Computed Tomography X-Ray", "SRT"), rel="HAS CONCEPT MOD"),
        item("CODE", ("121005", "Observer Type"), ("121007", "Device"), rel="HAS OBS CONTEXT"),
        item("UIDREF", ("121012", "Device Observer UID"), observer_uid,
             rel="HAS OBS CONTEXT"),
        item("DATETIME", ("113809", "Start of X-Ray Irradiation"), "20251106100500"),
        item("CONTAINER", ("113811", "CT Accumulated Dose Data")),
        *acqs,
    ])
    return ds


def proj_rdsr(study_uid="1.2.3.200", sop_uid="1.2.3.200.1"):
    ds = header(Dataset(), study_uid=study_uid, sop_uid=sop_uid, modality="SR",
                manufacturer="FLUOROCO", model="Fluoro 1", serial="F1", station="FL1")
    ds.SOPClassUID = RDSR
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = Sequence([code("113701", "X-Ray Radiation Dose Report")])

    def event(uid, etype, when, dap, rp, plane="113620", duration=None):
        ch = [
            item("CODE", ("113764", "Acquisition Plane"), (plane, "Plane")),
            item("DATETIME", ("111526", "DateTime Started"), when),
            item("CODE", ("113721", "Irradiation Event Type"), etype),
            item("TEXT", ("125203", "Acquisition Protocol"), "MCU 5-15kg"),
            item("UIDREF", ("113769", "Irradiation Event UID"), uid),
            item("NUM", ("122130", "Dose Area Product"), dap, "dGy.cm2"),
            item("NUM", ("113738", "Dose (RP)"), rp, "mGy"),
            item("NUM", ("113733", "KVP"), 70, "kV"),
            item("CONTAINER", ("113771", "X-Ray Filters"), children=[
                item("CODE", ("113757", "X-Ray Filter Material"), ("C-127F9", "Copper", "SRT")),
                item("NUM", ("113758", "X-Ray Filter Thickness Minimum"), 0.1, "mm"),
            ]),
        ]
        if duration is not None:
            ch.append(item("NUM", ("113742", "Irradiation Duration"), duration, "s"))
        return item("CONTAINER", ("113706", "Irradiation Event X-Ray Data"), children=ch)

    ds.ContentSequence = Sequence([
        item("CODE", ("121058", "Procedure reported"), ("113704", "Projection X-Ray")),
        event("f1", ("P5-06000", "Fluoroscopy", "SRT"), "20251106101000", 2.0, 5.0,
              duration=30),
        event("a1", ("113611", "Stationary Acquisition"), "20251106101100", 1.0, 2.0,
              plane="113621"),
    ])
    return ds


def dx_image(study_uid="1.2.3.300", sop_uid="1.2.3.300.1", sop_class=DX_PRES,
             manufacturer="ACME", model="Model X", serial="DX1", dap_dgycm2=0.5,
             acq_time="101500", **kw):
    ds = header(Dataset(), study_uid=study_uid, sop_uid=sop_uid, modality="DX",
                manufacturer=manufacturer, model=model, serial=serial, station="DXROOM", **kw)
    ds.SOPClassUID = sop_class
    ds.AcquisitionDate, ds.AcquisitionTime = "20251106", acq_time
    ds.ProtocolName = "Chest PA"
    ds.KVP = "80"
    ds.ExposureInuAs = "2500"
    ds.ExposureTime = "10"
    ds.ImageAndFluoroscopyAreaDoseProduct = str(dap_dgycm2)
    ds.ExposureIndex = "300"
    ds.RelativeXRayExposure = "1200"
    ds.DistanceSourceToDetector = "18"  # decimetres on the Kodak DR 7500
    ds.FilterMaterial = ["COPPER", "ALUMINUM"]
    ds.FilterThicknessMinimum = ["0.1", "1.0"]
    return ds


def text_report(study_uid="1.2.3.400", sop_uid="1.2.3.400.1"):
    ds = header(Dataset(), study_uid=study_uid, sop_uid=sop_uid, modality="SR")
    ds.SOPClassUID = TEXT_SR
    ds.ValueType = "CONTAINER"
    ds.ConceptNameCodeSequence = Sequence([code("18748-4", "Radiology Report", "LN")])
    ds.ContentSequence = Sequence([item("TEXT", ("121060", "History"), "no findings")])
    return ds


CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
PET = "1.2.840.10008.5.1.4.1.1.128"


def ct_image(study_uid="1.2.3.100", sop_uid="1.2.3.100.7", operator="RAD^ANA",
             physician="DOC^ION"):
    ds = header(Dataset(), study_uid=study_uid, sop_uid=sop_uid, modality="CT")
    ds.SOPClassUID = CT_IMAGE
    if operator:
        ds.OperatorsName = operator
    if physician:
        ds.PerformingPhysicianName = physician
    return ds


def ct_series_image(study_uid="1.2.3.600", sop_uid="1.2.3.600.1", series_uid="1.2.3.600.9",
                    ctdivol=10.0, count=50, spacing=5.0,
                    image_type=("ORIGINAL", "PRIMARY", "AXIAL"), acquisition="SPIRAL"):
    """A CT image whose header carries the series' dose values (no dose report exists)."""
    ds = ct_image(study_uid, sop_uid, operator=None, physician=None)
    ds.SeriesInstanceUID = series_uid
    ds.ImageType = list(image_type)
    ds.CTDIvol = ctdivol
    ds.KVP = 120
    ds.XRayTubeCurrent = 200
    ds.ExposureTime = 500
    ds.SpiralPitchFactor = 0.9
    ds.AcquisitionType = acquisition
    ds.SliceThickness = spacing
    ds.TotalCollimationWidth = 20.0
    ds.ProtocolName = "HEAD"
    if count:
        from dosimetrix.ingest.parse import mark_series_instances
        mark_series_instances(ds, count)
    return ds


def pet_image(study_uid="1.2.3.500", sop_uid="1.2.3.500.1", mbq=185.0, agent="Fluorodeoxyglucose"):
    ds = header(Dataset(), study_uid=study_uid, sop_uid=sop_uid, modality="PT")
    ds.SOPClassUID = PET
    rp = Dataset()
    rp.Radiopharmaceutical = agent
    rp.RadiopharmaceuticalStartDateTime = "20251106093000"
    rp.RadionuclideTotalDose = str(mbq * 1e6)
    rp.RadionuclideHalfLife = "6586.2"
    rp.RadionuclideCodeSequence = Sequence([code("C-111A1", "^18^Fluorine", "SRT")])
    ds.RadiopharmaceuticalInformationSequence = Sequence([rp])
    return ds
