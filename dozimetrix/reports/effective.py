"""CT effective dose report (DLP × k, see ``ctdose.py``): per exam and age group the distribution of
the estimated effective dose, and the collective dose per room. A population-level estimate for
comparing exams, rooms and periods, not a patient's dose. Needs the coefficients
(``dozimetrix ct-kfactor``); studies without one are counted and the reason given."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ctdose
from ..db.models import Classification, Code, CodeSystem, Event, Study
from ..stats import BandLookup, describe


def build(session: Session, conds: list) -> dict:
    from ..api.data import device_names

    table = ctdose.load(session)
    names = device_names(session)
    studies = list(session.scalars(select(Study).where(Study.modality.in_(("CT", "PT")), *conds)))
    ids = [s.id for s in studies]
    events: dict[int, list[Event]] = defaultdict(list)
    for i in range(0, len(ids), 1000):
        for e in session.scalars(select(Event).where(Event.study_id.in_(ids[i:i + 1000]))):
            events[e.study_id].append(e)
    regions: dict[int, str] = {}
    exam: dict[int, str] = {}
    system = session.scalar(select(CodeSystem.id).where(CodeSystem.key == ctdose.NATIONAL))
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        regions.update(ctdose.study_regions(session, chunk))
        for sid, code in session.execute(
                select(Classification.study_id, Code.code)
                .join(Code, (Code.id == Classification.code_id) & (Code.system_id == system))
                .where(Classification.study_id.in_(chunk), Classification.event_id.is_(None))):
            exam[sid] = code
    band = BandLookup(session, "ro-drl-age", "age")
    groups: dict[tuple, list[float]] = defaultdict(list)
    rooms: dict[int, list[float]] = defaultdict(list)
    skipped: Counter = Counter()
    n_partial = 0
    for s in studies:
        est = ctdose.estimate(s, events.get(s.id, []), regions.get(s.id), table)
        if est["msv"] is None:
            skipped[est["reason"]] += 1
            continue
        n_partial += not est["complete"]
        groups[(exam.get(s.id, "?"), band.label.get(band(s.age_days), "age unknown"))].append(
            est["msv"])
        rooms[s.device_id].append(est["msv"])
    rows = []
    for (code, b), v in groups.items():
        d = describe(v)
        rows.append({"exam": code, "band": b, "n": d["n"], "median_msv": round(d["median"], 2),
                     "p75_msv": round(d["p75"], 2), "max_msv": round(d["max"], 2),
                     "total_msv": round(sum(v), 1)})
    rows.sort(key=lambda r: (-r["total_msv"], r["exam"], r["band"]))
    room_rows = [{"device": names.get(d, f"Device {d}"), "n": len(v),
                  "mean_msv": round(sum(v) / len(v), 2), "collective_msv": round(sum(v), 1)}
                 for d, v in rooms.items()]
    room_rows.sort(key=lambda r: -r["collective_msv"])
    return {"studies": len(studies), "estimated": sum(len(v) for v in rooms.values()),
            "partial": n_partial, "coefficients": bool(table),
            "not_estimated": [{"reason": k, "n": v} for k, v in skipped.most_common()],
            "groups": rows, "rooms": room_rows,
            "note": "DLP × k, a population-level estimate; not for an individual patient"}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["exam", "age_group", "n", "median_msv", "p75_msv", "max_msv", "total_msv"])
    for r in rep["groups"]:
        w.writerow([r["exam"], r["band"], r["n"], r["median_msv"], r["p75_msv"], r["max_msv"],
                    r["total_msv"]])
    text.detach()
