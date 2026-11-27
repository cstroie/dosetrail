"""CT patient centering from an axial image: a synthetic phantom placed off the isocentre."""

import datetime as dt

import numpy as np
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from dozimetrix import centering
from dozimetrix.db.models import CtCentering, Device, Study
from dozimetrix.ingest.parse import parse
from dozimetrix.ingest.store import Store
from dozimetrix.reports import centering as report
from tests import dicom_factory as f

SECRET = b"t" * 32
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"


def phantom(cx_mm=0.0, cy_mm=0.0, *, dcc=True, position="HFS", size=256, spacing=2.0,
            study_uid="1.2.3.500", sop="1.2.3.500.1", table=False):
    """An elliptical body (HU 0, 300 x 200 mm) in air, centred at (cx, cy) mm from the isocentre,
    which sits at the origin of the patient coordinates; the image is centred on it."""
    from pydicom.dataset import Dataset

    ds = f.header(Dataset(), study_uid=study_uid, sop_uid=sop, modality="CT")
    ds.SOPClassUID = CT_IMAGE
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = CT_IMAGE
    ds.file_meta.MediaStorageSOPInstanceUID = sop
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.PatientPosition = position
    half = size * spacing / 2
    ds.ImagePositionPatient = [-half, -half, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [spacing, spacing]
    ds.Rows = ds.Columns = size
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 1
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.RescaleSlope, ds.RescaleIntercept = 1, 0
    yy, xx = np.mgrid[0:size, 0:size]
    px, py = (xx + 0.5) * spacing - half, (yy + 0.5) * spacing - half
    img = np.full((size, size), -1000, dtype=np.int16)
    img[((px - cx_mm) / 150) ** 2 + ((py - cy_mm) / 100) ** 2 <= 1] = 0
    if table:  # a dense table strip under the patient would shift the centroid
        img[(py > cy_mm + 100) & (py < cy_mm + 115) & (abs(px) < 200)] = 300
    ds.PixelData = img.tobytes()
    if dcc:
        ds.DataCollectionCenterPatient = [0.0, 0.0, 0.0]
    return ds


def test_offsets_from_the_data_collection_center():
    m = centering.measure(phantom(cx_mm=12.0, cy_mm=30.0))
    assert m["measured"] and m["method"] == "data_collection_center"
    assert m["offset_x_mm"] == pytest.approx(12.0, abs=3.0)
    assert m["offset_y_mm"] == pytest.approx(30.0, abs=3.0)  # + = lower than the isocentre
    assert m["body_w_mm"] == pytest.approx(300, abs=10)
    assert m["body_h_mm"] == pytest.approx(200, abs=10)
    c = centering.measure(phantom(0.0, 0.0))
    assert abs(c["offset_x_mm"]) < 3 and abs(c["offset_y_mm"]) < 3
    up = centering.measure(phantom(-4.0, -25.0))
    assert up["offset_y_mm"] < -20 and up["offset_x_mm"] < 0


def test_without_the_isocentre_in_the_header_the_image_centre_is_used_and_labelled():
    m = centering.measure(phantom(0.0, 20.0, dcc=False))
    assert m["method"] == "image_center" and m["offset_y_mm"] == pytest.approx(20.0, abs=3.0)
    n = centering.measure(phantom(0.0, 20.0, dcc=False), allow_image_center=False)
    assert not n["measured"] and "isocentre" in n["reason"]


def test_what_is_not_measured_says_why():
    assert "patient position" in centering.measure(phantom(position="HFP"))["reason"]
    ds = phantom()
    ds.ImageOrientationPatient = [0, 1, 0, 0, 0, -1]  # a sagittal reformat
    assert "not an axial" in centering.measure(ds)["reason"]
    air = phantom()
    air.PixelData = np.full((256, 256), -1000, dtype=np.int16).tobytes()
    assert "no body" in centering.measure(air)["reason"]
    gone = phantom()
    del gone.PixelData
    assert "pixels not readable" in centering.measure(gone)["reason"]


def test_off_by_default_and_stored_per_study(session, monkeypatch):
    # the default, whatever the machine's own TOML says
    monkeypatch.setattr("dozimetrix.config.load_settings",
                        lambda: type("S", (), {"measure_centering": False})())
    centering.enabled.cache_clear()
    assert not centering.enabled()  # nothing is read from pixels unless the TOML says so
    dev = Device(display_name="CT A", identity_key="ce-a", is_local=True)
    session.add(dev)
    session.flush()
    study = Study(study_uid="1.2.3.500", device_id=dev.id, modality="CT", source_kind="test",
                  study_datetime=dt.datetime(2026, 11, 20, 9))
    session.add(study)
    session.flush()
    ds = phantom(cx_mm=0.0, cy_mm=25.0)
    assert parse(ds).centering is None  # disabled: the image is parsed as before
    monkeypatch.setattr(centering, "enabled", lambda: True)
    obj = parse(ds)
    assert obj.centering["measured"] and obj.centering["study_uid"] == "1.2.3.500"
    store = Store(session, SECRET)
    store.store(obj)
    row = session.query(CtCentering).one()
    assert row.study_id == study.id and row.offset_y_mm == pytest.approx(25.0, abs=3.0)
    assert store.stats["centering_measured"] == 1
    # one per study: a later image does not overwrite a measurement
    other = parse(phantom(cx_mm=0.0, cy_mm=-30.0, sop="1.2.3.500.2"))
    store.store(other)
    assert session.query(CtCentering).one().offset_y_mm > 0
    # an image of a study we do not have is dropped
    store.store(parse(phantom(study_uid="9.9.9", sop="9.9.9.1")))
    assert store.stats["centering_no_study"] == 1
    # a localizer is not read
    loc = phantom(sop="1.2.3.500.3")
    loc.ImageType = ["ORIGINAL", "PRIMARY", "LOCALIZER"]
    assert parse(loc).centering is None


def test_report_per_room_and_method(session):
    dev = Device(display_name="CT A", identity_key="ce-b", is_local=True)
    session.add(dev)
    session.flush()
    for i, (y, method) in enumerate([(5, "data_collection_center"), (15, "data_collection_center"),
                                     (-25, "data_collection_center"), (12, "image_center")]):
        s = Study(study_uid=f"r.{i}", device_id=dev.id, modality="CT", source_kind="test",
                  study_datetime=dt.datetime(2026, 11, 20, 9 + i))
        session.add(s)
        session.flush()
        session.add(CtCentering(study_id=s.id, measured=True, offset_x_mm=1.0, offset_y_mm=float(y),
                                method=method))
    s = Study(study_uid="r.x", device_id=dev.id, modality="CT", source_kind="test",
              study_datetime=dt.datetime(2026, 11, 21))
    session.add(s)
    session.flush()
    session.add(CtCentering(study_id=s.id, measured=False, reason="pixels not readable: X"))
    session.flush()
    rep = report.build(session, [])
    assert (rep["measured"], rep["not_measured"]) == (4, 1)
    first = rep["rooms"][0]
    assert first["method"] == "data_collection_center" and first["n"] == 3
    assert first["median_vertical_mm"] == 5 and first["beyond_10_pct"] == pytest.approx(66.7)
    assert first["beyond_20_pct"] == pytest.approx(33.3)
    assert rep["rooms"][1]["method"] == "image_center"  # never mixed with the first
