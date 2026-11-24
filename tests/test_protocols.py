"""Protocol comparison: names, bands from names, grouping, dose rank."""

import datetime as dt

import pytest

from dozimetrix import classify, protocols
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
AGE_2Y = int(2 * 365.25) + 30


def test_name_band_and_family():
    assert protocols.name_band("CT_CAP_N_1_6_ANI") == ("age", pytest.approx(3.5 * 365.25))
    assert protocols.name_band("Torace 15-30 kg") == ("weight", 22.5)
    assert protocols.name_band("Torace 10kg") == ("weight", 10.0)
    assert protocols.name_band("3_12_luni")[0] == "age"
    assert protocols.name_band("CEREBRAL") is None
    assert protocols.family("CER_NATIV") == protocols.family("CER K") == "cer"
    assert protocols.family("CT_CAP_N_1_6_ANI") == "ct cap"


@pytest.fixture
def data(session):
    load_all(session)
    a = Device(display_name="CT A", identity_key="p-a", is_local=True)
    b = Device(display_name="CT B", identity_key="p-b", is_local=True)
    session.add_all([a, b])
    session.flush()
    rows = [(a, "CEREBRAL", 50.0, 1000.0, 130.0, AGE_2Y),
            (a, "CEREBRAL", 52.0, 1040.0, 130.0, AGE_2Y),
            (a, "Head K", 28.0, 520.0, 110.0, AGE_2Y), (b, "Head", 30.0, 560.0, 110.0, AGE_2Y),
            # no age: the band comes from the name
            (b, "CT_CAP_N_1_6_ANI", 20.0, 400.0, 100.0, None)]
    for i, (dev, proto, ctdi, dlp, kv, age) in enumerate(rows):
        s = Study(study_uid=f"p.{i}", modality="CT", device_id=dev.id, study_datetime=T,
                  study_description="CT cap nativ", source_kind="test", age_days=age)
        s.events = [Event(protocol="Topogram", is_localizer=True, dlp=5.0),
                    Event(protocol=proto, dlp=dlp, ctdivol=ctdi, kvp=kv, ma=200.0,
                          rotation_time_s=1.0, pitch=0.5, ct_phantom_cm=16)]
        session.add(s)
    session.flush()
    classify.run(session)


def test_compare(session, data):
    out = protocols.compare(session, [], "CT-CAP", min_n=1, by_device=False)
    assert out["parameter"] == "age"
    young = [r for r in out["rows"] if r["band"] == "age:1-6y"]
    by = {r["protocol"]: r for r in young}
    assert by["cerebral"]["n"] == 2 and by["cerebral"]["stats"]["ctdivol"]["median"] == 51
    # the name's band stands in for the missing age
    assert by["ct cap n 1 6 ani"]["n_from_name"] == 1
    # rank by median CTDIvol: lowest first
    ranked = sorted(young, key=lambda r: r["rank"])
    assert [r["protocol"] for r in ranked] == ["ct cap n 1 6 ani", "head k", "head", "cerebral"]
    assert ranked[0]["ratio_to_lowest"] == 1
    assert ranked[-1]["ratio_to_lowest"] == pytest.approx(2.55)
    assert by["cerebral"]["stats"]["emas"]["median"] == pytest.approx(400)  # 200 mA x 1 s / 0.5
    # per room, and merged variants
    per_room = protocols.compare(session, [], "CT-CAP", min_n=1)
    assert {r["device_name"] for r in per_room["rows"]} == {"CT A", "CT B"}
    fam = protocols.compare(session, [], "CT-CAP", min_n=1, by="family", by_device=False)
    assert "head" in {r["protocol"] for r in fam["rows"]}  # "Head K" folds into "Head"
    # a group below min_n is shown but not ranked
    few = protocols.compare(session, [], "CT-CAP", min_n=2, by_device=False)
    assert [r["rank"] for r in few["rows"] if r["protocol"] == "head"] == [None]
    with pytest.raises(protocols.ProtocolsError):
        protocols.compare(session, [], "NOPE")


def test_monitoring_left_out(session, data):
    dev = session.query(Device).filter_by(display_name="CT A").one()
    s = Study(study_uid="p.mon", modality="CT", device_id=dev.id, study_datetime=T,
              study_description="CT cap nativ", source_kind="test", age_days=AGE_2Y)
    s.events = [Event(protocol="Monitoring", event_type="stationary", dlp=9.0, ctdivol=9.0,
                      scan_length_mm=10.0),
                Event(protocol="Head", event_type="spiral", dlp=500.0, ctdivol=29.0,
                      scan_length_mm=180.0, ct_phantom_cm=16),
                Event(protocol="Scan aborted", event_type="spiral", dlp=18.0, ctdivol=29.0,
                      scan_length_mm=9.0)]
    session.add(s)
    session.flush()
    classify.run(session)
    out = protocols.compare(session, [], "CT-CAP", min_n=1, by_device=False)
    names = {r["protocol"] for r in out["rows"]}
    assert "monitoring" not in names and "scan aborted" not in names
    (head,) = [r for r in out["rows"] if r["protocol"] == "head" and r["band"] == "age:1-6y"]
    assert head["n"] == 2  # the fixture's and this one
