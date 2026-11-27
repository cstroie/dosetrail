"""CT scan length outliers per exam and age group."""

import datetime as dt

from dozimetrix import classify
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import scanlength
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)


def test_long_scans_are_flagged_per_group(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="sl-a", is_local=True)
    session.add(dev)
    session.flush()
    lengths = [180.0 + (i % 5) * 4 for i in range(30)] + [420.0]  # one into the neck
    for i, mm in enumerate(lengths):
        s = Study(study_uid=f"sl.{i}", modality="CT", device_id=dev.id, study_datetime=T,
                  study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test",
                  age_days=900, weight_kg=12.0)
        s.events = [Event(protocol="Topogram", is_localizer=True, scan_length_mm=500.0),
                    Event(protocol="Head", dlp=500.0, ctdivol=25.0, scan_length_mm=mm),
                    Event(protocol="Bolus monitor", dlp=1.0, scan_length_mm=30.0)]
        session.add(s)
    session.flush()
    classify.run(session)
    rep = scanlength.build(session, [])
    assert rep["flagged_total"] == 1
    (f,) = rep["flagged"]
    assert f["length_mm"] == 420 and f["ratio"] > 2 and f["device"] == "CT A"
    (g,) = rep["groups"]  # the monitoring series and the localizer are not compared
    assert (g["n"], g["flagged"]) == (31, 1) and g["median_mm"] == 188
    assert rep["rooms"][0]["flagged"] == 1
    assert scanlength.build(session, [], min_n=50)["groups"] == []
