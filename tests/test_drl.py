"""DRL comparison: bands, levels (series / exposure / study), units, status, per device."""

import datetime as dt

import pytest

from dosetrail import classify, drl
from dosetrail.db.models import Device, Event, Study
from dosetrail.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
AGE_2Y = int(2 * 365.25) + 30


@pytest.fixture
def data(session):
    load_all(session)
    a = Device(display_name="CT A", identity_key="d-a", is_local=True)
    b = Device(display_name="CT B", identity_key="d-b", is_local=True)
    rx = Device(display_name="Room 1", identity_key="d-rx", is_local=True)
    session.add_all([a, b, rx])
    session.flush()
    for i, (dev, dlp) in enumerate([(a, 600.0), (a, 1000.0), (b, 1300.0)]):
        s = Study(study_uid=f"d.{i}", modality="CT", device_id=dev.id, study_datetime=T,
                  study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test",
                  age_days=AGE_2Y)
        s.events = [Event(protocol="Topogram", is_localizer=True, dlp=5.0),
                    Event(protocol="Head", dlp=dlp, ctdivol=dlp / 20)]
        session.add(s)
    # skull AP twice in one study (a retake) + one lateral
    s = Study(study_uid="d.x", modality="DX", device_id=rx.id, study_datetime=T,
              study_description="Skull", source_kind="test", age_days=AGE_2Y)
    s.events = [Event(protocol="W001a Craniu a.p.", view="antero-posterior", dap=0.1),
                Event(protocol="W001a Craniu a.p.", view="antero-posterior", dap=0.05),
                Event(protocol="W001a Craniu lat.", view="lateral", dap=0.08)]
    session.add(s)
    session.flush()
    classify.run(session)
    return a, b


def _row(out, exam, band, quantity):
    (r,) = [r for r in out["rows"] if (r["exam"], r["band"], r["quantity"])
            == (exam, band, quantity)]
    return r


def test_national_set_per_series_and_per_study(session, data):
    out = drl.compare(session, [], "ro-ms838-2025", min_n=1)
    ct = _row(out, "CT-CAP", "1-4y", "dlp")  # reference 800 mGy.cm per series
    (g,) = ct["groups"]
    assert g["n"] == 3 and g["median"] == 1000.0  # localisers excluded
    assert g["ratio"] == pytest.approx(1.25) and ct["status"] == "above"
    ap = _row(out, "RX-CAP-AP", "1-4y", "dap")  # "study": the retake is included
    assert ap["groups"][0]["n"] == 1 and ap["groups"][0]["median"] == pytest.approx(0.15)
    assert _row(out, "CT-TORACE", "1-4y", "dlp")["status"] == "no_data"
    nm = _row(out, "NM-RENAL-DMSA", "1-4y", "activity")
    assert nm["status"] == "unavailable" and "activity" in nm["reason"]


def test_european_set_units_bands_and_devices(session, data):
    a, b = data
    out = drl.compare(session, [], "ec-rp185-2018", by_device=True, min_n=2)
    head = _row(out, "CT-HEAD", "1-6y", "dlp")  # 505 mGy.cm
    groups = {g["device_name"]: g for g in head["groups"]}
    assert groups["CT A"]["n"] == 2 and groups["CT A"]["median"] == 800.0
    assert groups["CT A"]["status"] == "well_above"  # 800 > 1.5 x 505
    assert groups["CT B"]["status"] == "insufficient"  # one study < min_n
    assert head["status"] == "well_above"
    lat = _row(out, "RX-HEAD-LAT", "1-6y", "dap")  # per exposure, in mGy.cm2
    assert lat["unit"] == "mGy.cm2" and lat["groups"][0]["median"] == pytest.approx(80.0)
    with pytest.raises(drl.DRLError):
        drl.compare(session, [], "nope")
