"""Repeat analysis for radiography: how often a projection is exposed again within one study.

**This is a proxy, not a reject rate.** A true reject rate needs the images the radiographer threw
away: DR consoles that export them (IHE "Rejected for Quality Reasons" key object notes, SOP class
1.2.840.10008.5.1.4.1.1.88.59) or the console's reject log. None reaches this system (no such object
has ever been received), so what is counted is a *possible repeat*: an exposure in a study that has
the same national exam code, projection, laterality and protocol as an earlier exposure of that
study, within ``gap`` minutes of it. It contains real retakes and also legitimate duplicates (two
exposures of a limb with the laterality not recorded, a deliberate second exposure), and it misses a
retake that was kept as a different protocol. The images that did *not* reach the dose report
(a rejected exposure some consoles never report) are not in it either; and the import drops an
exposure with the same second, DAP, kV and mAs as another (the same dose report received twice), so
two exposures that identical are counted once.

So the absolute rate is not to be quoted. What the report is for is the comparison: a room, exam or
operator whose rate is well above the department's for the same exam. A room is flagged at
``ratio`` (1.5) times the department's rate with at least ``min_n`` exposures.

Operators are named only on request (``by_operator``): the API allows it for administrators, as
the operator dose report does.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..classify import normalise, view_code
from ..db.models import Classification, Code, CodeSystem, Event, Study

NATIONAL = "ro-drl-ms838-2025"
GAP_MIN = 10.0
MIN_N = 50
RATIO = 1.5
# a stitched or multi-part exposure is several exposures of one projection on purpose
_STITCHED = re.compile(r"stitch|intreag|whole|scolioz|long ?leg|lung|full ?spine|full ?leg")


def _rate(repeats: int, n: int) -> float | None:
    return round(100.0 * repeats / n, 1) if n else None


def build(session: Session, conds: list, *, gap_min: float = GAP_MIN, min_n: int = MIN_N,
          ratio: float = RATIO, by_operator: bool = False) -> dict:
    from ..api.data import device_names

    system = session.scalar(select(CodeSystem.id).where(CodeSystem.key == NATIONAL))
    names = device_names(session)
    cols = [Study.id, Study.device_id, Code.code, Event.view, Event.protocol, Event.laterality,
            Event.event_datetime, Event.id, Study.operator_name]
    rows = session.execute(
        select(*cols).select_from(Event).join(Study, Study.id == Event.study_id)
        .join(Classification, Classification.event_id == Event.id)
        .join(Code, (Code.id == Classification.code_id) & (Code.system_id == system))
        .where(Study.modality.in_(("DX", "CR")), Event.is_localizer.is_not(True), *conds)).all()

    studies: dict[int, list] = defaultdict(list)
    for sid, device_id, code, view, protocol, lat, when, eid, operator in rows:
        text = normalise(protocol).strip()
        if _STITCHED.search(text):
            continue
        studies[sid].append((code, view_code(view, protocol), lat, text, when, eid, device_id,
                             operator))

    cell: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])  # (room, exam) -> n, repeats
    dept: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # exam -> n, repeats
    ops: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])  # (operator, exam)
    for ex in studies.values():
        ex.sort(key=lambda x: (x[4] is None, x[4], x[5]))
        last: dict[tuple, object] = {}
        for code, view, lat, text, when, _eid, device_id, operator in ex:
            key = (code, view, lat, text)
            again = False
            if key in last:
                prev = last[key]
                again = prev is None or when is None \
                    or (when - prev).total_seconds() <= gap_min * 60
            last[key] = when
            for table, k in ((cell, (device_id, code)), (dept, code),
                             *(((ops, (operator, code)),) if by_operator and operator else ())):
                table[k][0] += 1
                table[k][1] += again

    dept_rate = {c: v[1] / v[0] for c, v in dept.items() if v[0]}
    out = []
    for (device_id, code), (n, rep) in cell.items():
        base = dept_rate.get(code)
        r = rep / n if n else None
        flagged = bool(n >= min_n and base and r is not None and r >= ratio * base and rep >= 5)
        out.append({"device": names.get(device_id, f"Device {device_id}"), "exam": code,
                    "exposures": n, "repeats": rep, "rate_pct": _rate(rep, n),
                    "department_pct": round(100 * base, 1) if base else None,
                    "ratio": round(r / base, 2) if base and r is not None else None,
                    "flagged": flagged})
    out.sort(key=lambda x: (not x["flagged"], -(x["ratio"] or 0), x["device"], x["exam"]))
    exams = [{"exam": c, "exposures": v[0], "repeats": v[1], "rate_pct": _rate(v[1], v[0])}
             for c, v in dept.items()]
    exams.sort(key=lambda x: -x["exposures"])
    n_all, rep_all = sum(v[0] for v in dept.values()), sum(v[1] for v in dept.values())
    rep: dict = {"gap_min": gap_min, "min_n": min_n, "ratio": ratio, "exposures": n_all,
                 "repeats": rep_all, "rate_pct": _rate(rep_all, n_all), "exams": exams,
                 "rooms": out, "flagged": sum(x["flagged"] for x in out),
                 "true_rejects": "not available: no rejected-image notes are received"}
    if by_operator:
        rep["operators"] = sorted(
            ({"operator": o, "exam": c, "exposures": v[0], "repeats": v[1],
              "rate_pct": _rate(v[1], v[0]),
              "ratio": round(v[1] / v[0] / dept_rate[c], 2) if v[0] and dept_rate.get(c) else None}
             for (o, c), v in ops.items() if v[0] >= min_n),
            key=lambda x: -(x["ratio"] or 0))
    return rep


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["room", "exam", "exposures", "possible_repeats", "rate_pct", "department_pct",
                "ratio", "flagged"])
    for r in rep["rooms"]:
        w.writerow([r["device"], r["exam"], r["exposures"], r["repeats"], r["rate_pct"],
                    r["department_pct"], r["ratio"], "yes" if r["flagged"] else ""])
    text.detach()
