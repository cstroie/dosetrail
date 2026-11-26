"""Exposure index report (IEC 62494-1): the deviation index (DI) of digital radiography per room
and protocol, to catch dose creep and detector or AEC problems.

DI = 10 · log10(EI / EI_target): 0 is on target, +1 is 26 % more dose than the target, −1 is 20 %
less. The standard's action levels are used: above +3 (twice the target dose or more) the image
is overexposed, below −2 (a third less or worse) it is noisy. A room whose exposures sit
outside them repeatedly is the alert ``device_di`` (alerts.py). The target is the one the
detector reports with each exposure (per protocol, set by the site on the console): this report
shows it per protocol, and flags the protocols whose target is not the same in every exposure.
DI is taken from the DICOM header, else computed from EI and its target.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Event, Study
from ..protocols import family
from ..stats import describe, quantile

DI_HIGH, DI_LOW = 3.0, -2.0  # IEC 62494-1 / AAPM TG-116: overexposed above, noisy below


def deviation_index(di: float | None, ei: float | None, target: float | None) -> float | None:
    """The stored DI, else 10·log10(EI / target)."""
    if di is not None:
        return di
    if ei and target and ei > 0 and target > 0:
        return 10.0 * math.log10(ei / target)
    return None


def outside(di: float) -> bool:
    return di > DI_HIGH or di < DI_LOW


def build(session: Session, conds: list) -> dict:
    """DI per room and protocol family among the studies ``conds`` selects."""
    from ..api.data import device_names

    names = device_names(session)
    rows = session.execute(
        select(Study.device_id, Event.protocol, Event.deviation_index, Event.exposure_index,
               Event.target_exposure_index)
        .select_from(Event).join(Study, Study.id == Event.study_id)
        .where(Event.is_localizer.is_not(True), *conds,
               (Event.deviation_index.is_not(None)) | (Event.exposure_index.is_not(None)))).all()
    cells: dict[tuple, dict] = defaultdict(lambda: {"di": [], "targets": []})
    for device_id, protocol, di, ei, target in rows:
        v = deviation_index(di, ei, target)
        if v is None:
            continue
        c = cells[(device_id, family(protocol) or "(no protocol)")]
        c["di"].append(v)
        if target:
            c["targets"].append(round(target))
    out = []
    for (device_id, protocol), c in cells.items():
        v = sorted(c["di"])
        n = len(v)
        hi = sum(x > DI_HIGH for x in v)
        lo = sum(x < DI_LOW for x in v)
        t = describe(c["targets"]) if c["targets"] else {}
        out.append({"device": names.get(device_id, f"Device {device_id}"), "protocol": protocol,
                    "n": n, "median_di": round(quantile(v, 0.5), 2),
                    "p5_di": round(quantile(v, 0.05), 2), "p95_di": round(quantile(v, 0.95), 2),
                    "above_pct": round(100.0 * hi / n, 1), "below_pct": round(100.0 * lo / n, 1),
                    "outside_pct": round(100.0 * (hi + lo) / n, 1),
                    "target_ei": t.get("median"), "targets_differ": len(set(c["targets"])) > 1})
    out.sort(key=lambda r: (-r["outside_pct"] * (r["n"] >= 10), -r["n"], r["device"]))
    n = sum(r["n"] for r in out)
    n_out = sum(round(r["outside_pct"] * r["n"] / 100) for r in out)
    return {"exposures": n, "outside": n_out, "high": DI_HIGH, "low": DI_LOW, "rows": out}


def write_csv(rep: dict, target: IO[bytes]) -> None:
    import io

    text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="", write_through=True)
    w = csv.writer(text)
    w.writerow(["room", "protocol", "exposures", "median_di", "p5_di", "p95_di",
                f"above_{rep['high']:g}_pct", f"below_{rep['low']:g}_pct", "outside_pct",
                "target_ei", "targets_differ"])
    for r in rep["rows"]:
        w.writerow([r["device"], r["protocol"], r["n"], r["median_di"], r["p5_di"], r["p95_di"],
                    r["above_pct"], r["below_pct"], r["outside_pct"], r["target_ei"],
                    "yes" if r["targets_differ"] else ""])
    text.detach()
