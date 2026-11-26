"""CT scan length: outliers per exam and age group, the sign of a scan that runs past its region
(a head scan into the neck, an abdomen scan extended into the chest).

The scanned length of each diagnostic series is compared with the department's median for its
national exam code and age group; a series is flagged when it is at least ``ratio`` × the median
and above the group's P95. Per room the report gives the share flagged, which tells a habit
(one room, one operator's protocol edit) from a one-off. Overranging in the strict sense (the
extra rotations a helical scan adds at both ends) is not in the dose report and not measured:
the length is the planned scan range plus whatever the operator added.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from typing import IO

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..classify import normalise
from ..db.models import Classification, Code, CodeSystem, Event, Study
from ..protocols import _NOT_DIAGNOSTIC, MIN_SCAN_MM
from ..stats import BandLookup, quantile

NATIONAL = "ro-drl-ms838-2025"
MIN_N = 20
RATIO = 1.3


def build(session: Session, conds: list, min_n: int = MIN_N, ratio: float = RATIO,
          limit: int = 200) -> dict:
    from ..api.data import device_names

    system = session.scalar(select(CodeSystem.id).where(CodeSystem.key == NATIONAL))
    names = device_names(session)
    band = BandLookup(session, "ro-drl-age", "age")
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    rows = session.execute(
        select(Study.id, Study.device_id, Study.study_datetime, Study.age_days, Code.code,
               Event.protocol, Event.scan_length_mm)
        .select_from(Event).join(Study, Study.id == Event.study_id)
        .join(Classification, (Classification.study_id == Study.id)
              & Classification.event_id.is_(None))
        .join(Code, (Code.id == Classification.code_id) & (Code.system_id == system))
        .where(Study.modality.in_(("CT", "PT")), Event.is_localizer.is_not(True),
               Event.scan_length_mm >= MIN_SCAN_MM,
               or_(Event.event_type.is_(None), Event.event_type != "stationary"), *conds))
    for sid, device_id, when, age, code, protocol, length in rows:
        b = band(age)
        if b is None or _NOT_DIAGNOSTIC.search(normalise(protocol)):
            continue
        groups[(code, b)].append((length, sid, device_id, when, protocol))

    table, flagged = [], []
    room_n: dict[int, int] = defaultdict(int)
    room_out: dict[int, int] = defaultdict(int)
    for (code, b), v in groups.items():
        if len(v) < min_n:
            continue
        lengths = sorted(x[0] for x in v)
        med, p95 = quantile(lengths, 0.5), quantile(lengths, 0.95)
        out = [x for x in v if x[0] >= ratio * med and x[0] > p95]
        for x in v:
            room_n[x[2]] += 1
        for x in out:
            room_out[x[2]] += 1
            flagged.append({"study_id": x[1], "device": names.get(x[2], f"Device {x[2]}"),
                            "time": x[3].isoformat(timespec="minutes") if x[3] else None,
                            "exam": code, "band": band.label.get(b, b), "protocol": x[4],
                            "length_mm": round(x[0]), "median_mm": round(med),
                            "ratio": round(x[0] / med, 2)})
        table.append({"exam": code, "band": band.label.get(b, b), "n": len(v),
                      "median_mm": round(med), "p75_mm": round(quantile(lengths, 0.75)),
                      "p95_mm": round(p95), "max_mm": round(lengths[-1]), "flagged": len(out)})
    table.sort(key=lambda r: (-r["flagged"], r["exam"], r["band"]))
    flagged.sort(key=lambda r: -r["ratio"])
    rooms = [{"device": names.get(d, f"Device {d}"), "series": n, "flagged": room_out[d],
              "flagged_pct": round(100.0 * room_out[d] / n, 1)} for d, n in room_n.items()]
    rooms.sort(key=lambda r: (-r["flagged_pct"], r["device"]))
    return {"min_n": min_n, "ratio": ratio, "groups": table, "rooms": rooms,
            "flagged_total": len(flagged), "flagged": flagged[:limit]}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["study_id", "room", "time", "exam", "age_group", "protocol", "length_mm",
                "median_mm", "ratio"])
    for r in rep["flagged"]:
        w.writerow([r["study_id"], r["device"], r["time"], r["exam"], r["band"], r["protocol"],
                    r["length_mm"], r["median_mm"], r["ratio"]])
    text.detach()
