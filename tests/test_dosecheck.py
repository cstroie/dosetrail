"""CT Dose Check report: notifications and alerts per protocol and room, with the reasons."""

import datetime as dt
import io

from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import dosecheck

WHEN = dt.datetime(2026, 10, 5, 9, 0)


def _ct(session, dev, uid, events):
    s = Study(study_uid=uid, device_id=dev.id, modality="CT", study_datetime=WHEN,
              source_kind="test")
    s.events = events
    session.add(s)
    session.flush()


def test_report_counts_per_protocol_room_and_reason(session):
    a = Device(display_name="CT A", identity_key="dc-a", is_local=True)
    b = Device(display_name="CT B", identity_key="dc-b", is_local=True)
    session.add_all([a, b])
    session.flush()
    plain = lambda: Event(protocol="CAP N", dlp=300.0, ctdivol=5.0)  # noqa: E731
    note = Event(protocol="CAP C", dlp=900.0, dose_check_notification=True,
                 extra_json={"dose_check": {"fwd_dlp": 1350.0, "reason": "Trauma "}})
    alert = Event(protocol="CAP C", dlp=1500.0, dose_check_alert=True,
                  extra_json={"dose_check": {"alert_fwd_dlp": 2100.0}})
    loc = Event(protocol="Topogram", is_localizer=True, dose_check_notification=True)
    _ct(session, a, "dc.1", [plain(), note, loc])
    _ct(session, a, "dc.2", [plain(), alert])
    _ct(session, b, "dc.3", [Event(protocol="HEAD N", dlp=800.0)])
    rep = dosecheck.build(session, [Study.study_datetime >= WHEN - dt.timedelta(days=1)])
    assert (rep["events"], rep["notifications"], rep["alerts"]) == (5, 1, 1)
    top = rep["protocols"][0]
    assert top["protocol"] == "cap" and top["events"] == 4  # "CAP N" and "CAP C" merge
    assert (top["notifications"], top["alerts"], top["rate_pct"]) == (1, 1, 50.0)
    assert top["max_fwd_dlp"] == 2100.0
    assert [(r["device"], r["events"], r["notifications"] + r["alerts"])
            for r in rep["rooms"]] == [("CT A", 4, 2), ("CT B", 1, 0)]
    assert {r["reason"]: r["n"] for r in rep["reasons"]} == {"trauma": 1, "(none recorded)": 1}
    out = io.BytesIO()
    dosecheck.write_csv(rep, out)
    assert out.getvalue().decode("utf-8-sig").startswith("protocol,acquisitions,")
