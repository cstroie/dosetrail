"""Exposure index report: DI per room and protocol."""

import datetime as dt
import io

from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import exposure

WHEN = dt.datetime(2026, 10, 5, 9, 0)


def test_di_per_room_and_protocol(session):
    dev = Device(display_name="DR 1", identity_key="ei-1", is_local=True)
    session.add(dev)
    session.flush()
    events = ([Event(protocol="Torace PA", deviation_index=0.1, exposure_index=300,
                     target_exposure_index=300)] * 8
              + [Event(protocol="Torace PA N", exposure_index=700, target_exposure_index=300),
                 Event(protocol="Torace PA", deviation_index=-3.0, target_exposure_index=250),
                 Event(protocol="Cap", dap=0.1),  # no EI at all: left out
                 Event(protocol="Topogram", is_localizer=True, deviation_index=9.0)])
    s = Study(study_uid="ei.1", device_id=dev.id, modality="DX", study_datetime=WHEN,
              source_kind="test")
    s.events = [Event(**{c.key: getattr(e, c.key) for c in e.__table__.columns
                         if c.key not in ("id", "study_id") and getattr(e, c.key) is not None})
                for e in events]
    session.add(s)
    session.flush()
    rep = exposure.build(session, [Study.study_datetime >= WHEN - dt.timedelta(days=1)])
    assert rep["exposures"] == 10 and rep["outside"] == 2
    row = rep["rows"][0]
    assert (row["device"], row["protocol"], row["n"]) == ("DR 1", "torace pa", 10)
    assert (row["above_pct"], row["below_pct"], row["outside_pct"]) == (10.0, 10.0, 20.0)
    assert row["targets_differ"] and row["target_ei"] in (300, 250, 300.0)
    out = io.BytesIO()
    exposure.write_csv(rep, out)
    assert out.getvalue().decode("utf-8-sig").startswith("room,protocol,exposures")
