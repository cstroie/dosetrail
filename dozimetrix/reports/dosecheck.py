"""CT Dose Check report (NEMA XR-25): which acquisitions went past the notification and alert
values the scanner is configured with, how often per protocol and room, and the reason the
operator gave for proceeding.

The RDSR carries it per acquisition (TID 10015, read by ``ingest/parse.py``): a notification is
raised when the forward estimate passes the configured notification value, an alert when it
passes the alert value (which needs an authorised person to proceed). ``event.dose_check_alert``
and ``event.dose_check_notification`` mark them, the values and the reason are in
``extra_json["dose_check"]``. Only scanners that export the Dose Check details appear; a room with
no event of either kind may have the function off, or may not export it.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from typing import IO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..classify import normalise
from ..db.models import Event, Study
from ..protocols import family
from ..stats import describe


def _rate(n: int, total: int) -> float | None:
    return round(100.0 * n / total, 2) if total else None


def build(session: Session, conds: list) -> dict:
    """Dose Check notifications and alerts among the CT acquisitions of the studies ``conds``
    selects (put the period in conds)."""
    from ..api.data import device_names

    base = [Study.modality.in_(("CT", "PT")), Event.is_localizer.is_not(True), *conds]
    # the denominator: every acquisition, per protocol family and room
    totals: Counter = Counter()
    rooms_total: Counter = Counter()
    for protocol, device_id, n in session.execute(
            select(Event.protocol, Study.device_id, func.count()).select_from(Event)
            .join(Study, Study.id == Event.study_id).where(*base)
            .group_by(Event.protocol, Study.device_id)):
        totals[family(protocol) or "(no protocol)"] += n
        rooms_total[device_id] += n

    flagged = session.execute(
        select(Event.protocol, Study.device_id, Event.dose_check_notification,
               Event.dose_check_alert, Event.extra_json, Event.dlp)
        .select_from(Event).join(Study, Study.id == Event.study_id)
        .where(*base, (Event.dose_check_notification.is_(True))
               | (Event.dose_check_alert.is_(True)))).all()

    names = device_names(session)
    by_protocol: dict[str, dict] = defaultdict(lambda: {"notifications": 0, "alerts": 0,
                                                        "fwd_dlp": []})
    by_room: dict[int, dict] = defaultdict(lambda: {"notifications": 0, "alerts": 0})
    reasons: Counter = Counter()
    for protocol, device_id, note, alert, extra, _dlp in flagged:
        dc = (extra or {}).get("dose_check") or {}
        row = by_protocol[family(protocol) or "(no protocol)"]
        room = by_room[device_id]
        for r, hit in ((row, note), (room, note)):
            r["notifications"] += bool(hit)
        for r, hit in ((row, alert), (room, alert)):
            r["alerts"] += bool(hit)
        fwd = dc.get("fwd_dlp") or dc.get("alert_fwd_dlp")
        if fwd:
            row["fwd_dlp"].append(fwd)
        reasons[normalise(dc.get("reason")).strip() or "(none recorded)"] += 1

    protocols = []
    for name, r in by_protocol.items():
        n = totals.get(name, 0)
        hits = r["notifications"] + r["alerts"]
        d = describe(r["fwd_dlp"]) if r["fwd_dlp"] else {}
        protocols.append({"protocol": name, "events": n, "notifications": r["notifications"],
                          "alerts": r["alerts"], "rate_pct": _rate(hits, n),
                          "median_fwd_dlp": d.get("median"), "max_fwd_dlp": d.get("max")})
    protocols.sort(key=lambda x: (-(x["notifications"] + x["alerts"]), x["protocol"]))
    rooms = []
    for device_id, n in rooms_total.items():
        r = by_room.get(device_id, {"notifications": 0, "alerts": 0})
        rooms.append({"device": names.get(device_id, f"Device {device_id}"), "events": n,
                      "notifications": r["notifications"], "alerts": r["alerts"],
                      "rate_pct": _rate(r["notifications"] + r["alerts"], n)})
    rooms.sort(key=lambda x: (-(x["notifications"] + x["alerts"]), x["device"]))
    n_note = sum(r["notifications"] for r in by_room.values())
    n_alert = sum(r["alerts"] for r in by_room.values())
    return {"events": sum(totals.values()), "notifications": n_note, "alerts": n_alert,
            "protocols": protocols, "rooms": rooms,
            "reasons": [{"reason": k, "n": v} for k, v in reasons.most_common()]}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["protocol", "acquisitions", "notifications", "alerts", "rate_pct",
                "median_forward_dlp_mGycm", "max_forward_dlp_mGycm"])
    for r in rep["protocols"]:
        w.writerow([r["protocol"], r["events"], r["notifications"], r["alerts"],
                    r["rate_pct"], r["median_fwd_dlp"], r["max_fwd_dlp"]])
    w.writerow([])
    w.writerow(["room", "acquisitions", "notifications", "alerts", "rate_pct"])
    for r in rep["rooms"]:
        w.writerow([r["device"], r["events"], r["notifications"], r["alerts"], r["rate_pct"]])
    w.writerow([])
    w.writerow(["reason", "n"])
    for r in rep["reasons"]:
        w.writerow([r["reason"], r["n"]])
    text.detach()
