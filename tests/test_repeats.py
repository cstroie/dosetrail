"""Repeat analysis (a proxy for the reject rate): possible repeats per room and exam."""

import datetime as dt

from dozimetrix import classify
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import repeats
from dozimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
CHEST = "W035 Torace a.p."


def _room(session, name, n_studies, retake_every, protocol=CHEST, operator="OP^ONE"):
    dev = Device(display_name=name, identity_key=f"rp-{name}", is_local=True)
    session.add(dev)
    session.flush()
    for i in range(n_studies):
        when = T + dt.timedelta(hours=i)
        s = Study(study_uid=f"{name}.{i}", modality="DX", device_id=dev.id, study_datetime=when,
                  study_description="Rx", source_kind="test", age_days=9000,
                  operator_name=operator)
        s.events = [Event(protocol=protocol, view="antero-posterior", dap=0.1 + i * 1e-4,
                          event_datetime=when)]
        if retake_every and i % retake_every == 0:  # a second exposure, 3 minutes later
            s.events.append(Event(protocol=protocol, view="antero-posterior", dap=0.12,
                                  event_datetime=when + dt.timedelta(minutes=3)))
        session.add(s)
    return dev


def test_repeats_are_compared_with_the_department(session):
    load_all(session)
    _room(session, "A", 80, 0)  # never retakes
    _room(session, "B", 80, 0)
    _room(session, "Bad", 80, 4, operator="OP^TWO")  # a retake in every 4th study
    session.flush()
    classify.run(session)
    rep = repeats.build(session, [], by_operator=True)
    assert rep["exposures"] == 260 and rep["repeats"] == 20
    got = {r["device"]: r for r in rep["rooms"]}
    assert got["A"]["repeats"] == 0 and not got["A"]["flagged"]
    assert (got["Bad"]["exposures"], got["Bad"]["repeats"]) == (100, 20)
    assert got["Bad"]["rate_pct"] == 20.0 and got["Bad"]["flagged"] and rep["flagged"] == 1
    assert rep["rooms"][0]["device"] == "Bad"  # the flagged first
    assert rep["true_rejects"].startswith("not available")
    assert [o["operator"] for o in rep["operators"]][0] == "OP^TWO"
    assert "operators" not in repeats.build(session, [])  # staff names only on request


def test_a_distant_second_exposure_and_stitching_do_not_count(session):
    load_all(session)
    dev = Device(display_name="C", identity_key="rp-c", is_local=True)
    session.add(dev)
    session.flush()
    s = Study(study_uid="c.1", modality="DX", device_id=dev.id, study_datetime=T,
              source_kind="test", age_days=9000)
    s.events = [Event(protocol=CHEST, view="antero-posterior", dap=0.1, event_datetime=T),
                Event(protocol=CHEST, view="antero-posterior", dap=0.1,
                      event_datetime=T + dt.timedelta(hours=2)),  # later: a new request
                Event(protocol="Coloana intreaga", view="antero-posterior", dap=0.2,
                      event_datetime=T),
                Event(protocol="Coloana intreaga", view="antero-posterior", dap=0.3,
                      event_datetime=T + dt.timedelta(minutes=1))]
    session.add(s)
    session.flush()
    classify.run(session)
    rep = repeats.build(session, [])
    assert rep["repeats"] == 0
    assert repeats.build(session, [], gap_min=180)["repeats"] == 1
