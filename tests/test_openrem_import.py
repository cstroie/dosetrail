"""OpenREM importer on a small synthetic dump (no real data)."""

import csv
import gzip
import hashlib

from sqlalchemy import func, select

from dosetrail.db.models import Device, Event, Patient, Study
from dosetrail.legacy.openrem.importer import OpenREMImporter
from dosetrail.legacy.openrem.reconcile import reconcile

STUDY_COLS = [
    "orem_id", "study_instance_uid", "study_date", "study_time", "accession_number",
    "accession_hashed", "study_description", "procedure_code_meaning",
    "requested_procedure_code_meaning", "performing_physician_name", "operator_name",
    "referring_physician_name", "modality_type", "number_of_events", "total_dlp", "total_dap",
    "total_rp_dose_a", "total_rp_dose_b", "total_agd_left", "total_agd_right", "patient_name",
    "name_hashed", "patient_id", "id_hashed", "patient_birth_date", "patient_sex",
    "not_patient_indicator", "patient_age", "patient_age_decimal", "patient_size",
    "patient_weight", "manufacturer", "manufacturer_model_name", "device_serial_number",
    "station_name", "institution_name", "institutional_department_name", "software_versions",
    "gantry_id", "unique_equipment_name_id", "display_name", "user_defined_modality",
    "device_observer_uid", "total_fluoro_time", "fluoro_dap", "acq_dap",
]


def _write(path, cols, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _study(i, uid, **kw):
    base = dict(orem_id=i, study_instance_uid=uid, study_date="2025-03-10", study_time="10:00:00",
                modality_type="DX", manufacturer="SIEMENS", manufacturer_model_name="Fluorospot",
                device_serial_number="999", station_name="YSIO-1", display_name="Room 1",
                id_hashed="f", name_hashed="f")
    base.update(kw)
    return base


def make_dump(tmp_path):
    h = hashlib.sha256(b"P123").hexdigest()
    studies = [
        # hashed historical record and clear record of the same patient
        _study(1, "1.2.1", patient_id=h, id_hashed="t", patient_name="x" * 64, name_hashed="t",
               study_date="2019-05-01", patient_age="004Y"),
        _study(2, "1.2.2", patient_id="P123", patient_name="POPESCU^ION",
               patient_birth_date="2015-01-20", patient_weight="21.5", station_name="ysio-1"),
        # same Study UID twice in OpenREM (continued study) -> merged
        _study(3, "1.2.3", patient_id="P9"),
        _study(4, "1.2.3", patient_id="P9"),
        _study(5, "1.3.1", modality_type="CT", manufacturer="SIEMENS", device_serial_number="77",
               station_name="CT1", patient_id="P9"),
    ]
    _write(tmp_path / "studies.csv.gz", STUDY_COLS, studies)
    pcols = ["orem_study_id", "orem_event_id", "irradiation_event_uid", "started",
             "acquisition_protocol", "comment", "dose_area_product", "event_type", "plane",
             "exposure_uas", "exposure_time", "kvp", "filters", "laterality"]
    _write(tmp_path / "proj_events.csv.gz", pcols, [
        dict(orem_study_id=1, irradiation_event_uid="e1", dose_area_product="1.5e-06",
             event_type="Stationary Acquisition", exposure_uas="2600", exposure_time="5",
             kvp="70", filters="Copper:0.1;Aluminum:1.0"),
        dict(orem_study_id=2, irradiation_event_uid="e2", dose_area_product="2e-06"),
        dict(orem_study_id=3, irradiation_event_uid="e3", dose_area_product="1e-06"),
        dict(orem_study_id=4, irradiation_event_uid="e3", dose_area_product="1e-06"),  # dup
        dict(orem_study_id=4, irradiation_event_uid="e4", dose_area_product="1e-06"),
    ])
    ccols = ["orem_study_id", "orem_event_id", "irradiation_event_uid", "started",
             "acquisition_protocol", "acquisition_type", "phantom", "mean_ctdivol", "dlp",
             "scanning_length"]
    _write(tmp_path / "ct_events.csv.gz", ccols, [
        dict(orem_study_id=5, irradiation_event_uid="c1", acquisition_type="Constant Angle "
             "Acquisition", dlp="5", mean_ctdivol="0.2"),
        dict(orem_study_id=5, irradiation_event_uid="c2", acquisition_type="Spiral Acquisition",
             phantom="IEC Head Dosimetry Phantom 16 cm", dlp="500", mean_ctdivol="40"),
    ])
    _write(tmp_path / "object_uids.csv.gz", ["sop_instance_uid", "orem_study_id"],
           [{"sop_instance_uid": "9.9.1", "orem_study_id": 1}])
    scols = ["modality", "year", "studies", "uids", "sum_total_dlp", "sum_total_dap_gym2",
             "ct_events", "sum_event_dlp", "proj_events", "sum_event_dap_gym2"]
    _write(tmp_path / "summary.csv.gz", scols, [
        dict(modality="CT", year=2025, studies=1, uids=1, ct_events=2, sum_event_dlp=505,
             proj_events=0),
        dict(modality="DX", year=2019, studies=1, uids=1, ct_events=0, proj_events=1,
             sum_event_dap_gym2="1.5e-06"),
        # OpenREM counts the duplicated event e3 twice; DoseTrail keeps it once -> DIFF
        dict(modality="DX", year=2025, studies=3, uids=2, ct_events=0, proj_events=4,
             sum_event_dap_gym2="5e-06"),
    ])
    return tmp_path


def test_import(session, tmp_path):
    imp = OpenREMImporter(session, make_dump(tmp_path))
    stats = imp.run(progress=lambda _m: None)
    session.flush()

    assert stats.studies_new == 4 and stats.studies_merged == 1
    assert stats.events_duplicate == 1
    # patient linkage: hashed + clear -> one patient with clear id and legacy hash
    p = session.scalar(select(Patient).filter_by(patient_id="P123"))
    assert p.legacy_sha256 == hashlib.sha256(b"P123").hexdigest()
    assert p.name == "POPESCU^ION"
    assert session.scalar(select(func.count()).select_from(Study)
                          .where(Study.patient_id == p.id)) == 2
    # device case variants collapse (same manufacturer + serial)
    assert session.scalar(select(func.count()).select_from(Device)
                          .where(Device.serial_number == "999")) == 1
    # unit conversions
    e1 = session.scalar(select(Event).filter_by(event_uid="e1"))
    assert abs(e1.dap - 0.015) < 1e-12  # 1.5e-6 Gy·m² -> 0.015 Gy·cm²
    assert e1.mas == 2.6 and e1.exposure_time_s == 0.005
    assert e1.filter_cu_mm == 0.1 and e1.filter_al_mm == 1.0
    # ages: exact from birth date; DICOM age as fallback
    s2 = session.scalar(select(Study).filter_by(study_uid="1.2.2"))
    assert s2.age_precision == "exact" and s2.weight_kg == 21.5
    # CT: localizer flag, phantom, totals
    ct = session.scalar(select(Study).filter_by(study_uid="1.3.1"))
    assert ct.n_events == 2 and ct.total_dlp == 505
    loc = session.scalar(select(Event).filter_by(event_uid="c1"))
    assert loc.is_localizer and loc.event_type == "localizer"
    assert session.scalar(select(Event).filter_by(event_uid="c2")).ct_phantom_cm == 16

    lines, ok = reconcile(session, tmp_path)
    assert not ok  # the duplicated OpenREM event is reported, not silently hidden
    assert any("DX" in line and "DIFF" in line for line in lines)
    assert any("CT" in line and "OK" in line for line in lines)
