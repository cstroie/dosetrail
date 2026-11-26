"""CT centering report: per room, how far patients lay from the isocentre (see ``centering.py``).

Vertical offset + = the patient lies lower than the isocentre. Per room: how many studies were
measured, the median and spread of the vertical and lateral offsets, and the share beyond 10 and
20 mm. Measurements with ``method = image_center`` (the image centre taken as the isocentre) are
reported apart from those with the scanner's own Data Collection Center and are not mixed: only
the latter are as trustworthy as the header. Nothing is measured unless
``[ingest] measure_centering`` was on when the images were received."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..centering import LIMIT_MM
from ..db.models import CtCentering, Study
from ..stats import describe


def build(session: Session, conds: list) -> dict:
    from ..api.data import device_names

    names = device_names(session)
    rows = session.execute(
        select(Study.device_id, CtCentering.measured, CtCentering.reason, CtCentering.method,
               CtCentering.offset_x_mm, CtCentering.offset_y_mm)
        .join(Study, Study.id == CtCentering.study_id).where(*conds)).all()
    cells: dict[tuple, dict] = defaultdict(lambda: {"x": [], "y": []})
    failed: Counter = Counter()
    for device_id, measured, reason, method, x, y in rows:
        if not measured:
            failed[reason or "not measured"] += 1
            continue
        c = cells[(device_id, method)]
        c["x"].append(x)
        c["y"].append(y)
    out = []
    for (device_id, method), c in cells.items():
        ay = [abs(v) for v in c["y"]]
        n = len(ay)
        dy, dx = describe(c["y"]), describe(c["x"])
        out.append({"device": names.get(device_id, f"Device {device_id}"), "method": method,
                    "n": n, "median_vertical_mm": round(dy["median"], 1),
                    "p25_vertical_mm": round(dy["p25"], 1), "p75_vertical_mm": round(dy["p75"], 1),
                    "median_lateral_mm": round(dx["median"], 1),
                    "beyond_10_pct": round(100 * sum(a > LIMIT_MM[0] for a in ay) / n, 1),
                    "beyond_20_pct": round(100 * sum(a > LIMIT_MM[1] for a in ay) / n, 1)})
    out.sort(key=lambda r: (r["method"] != "data_collection_center", -r["beyond_20_pct"],
                            r["device"]))
    return {"measured": sum(r["n"] for r in out), "not_measured": sum(failed.values()),
            "reasons": [{"reason": k, "n": v} for k, v in failed.most_common()],
            "limits_mm": list(LIMIT_MM), "rooms": out,
            "note": "unverified on real images; image_center rows rest on an assumption"}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["room", "method", "n", "median_vertical_mm", "p25_vertical_mm",
                "p75_vertical_mm", "median_lateral_mm", "beyond_10_pct", "beyond_20_pct"])
    for r in rep["rooms"]:
        w.writerow([r["device"], r["method"], r["n"], r["median_vertical_mm"],
                    r["p25_vertical_mm"], r["p75_vertical_mm"], r["median_lateral_mm"],
                    r["beyond_10_pct"], r["beyond_20_pct"]])
    text.detach()
