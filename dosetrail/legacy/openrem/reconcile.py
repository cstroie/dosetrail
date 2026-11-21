"""Compare imported data with OpenREM's own summary (computed independently at dump time)."""

from __future__ import annotations

import csv
import gzip
from collections import defaultdict
from pathlib import Path

from sqlalchemy import case, extract, func, select
from sqlalchemy.orm import Session

from ...db.models import Event, Study

DAP_GYM2_TO_GYCM2 = 1e4


def _source(dump_dir: Path) -> dict[tuple[str, int], dict]:
    out = {}
    with gzip.open(dump_dir / "summary.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            out[(r["modality"] or "OT", int(r["year"]))] = {
                "studies": int(r["uids"]),
                "events": int(r["ct_events"]) + int(r["proj_events"]),
                "dlp": _f(r["sum_event_dlp"]),
                "dap": _f(r["sum_event_dap_gym2"]) * DAP_GYM2_TO_GYCM2,
            }
    return out


def _target(session: Session) -> dict[tuple[str, int], dict]:
    year = func.coalesce(extract("year", Study.study_datetime), 0)
    out: dict = defaultdict(lambda: {"studies": 0, "events": 0, "dlp": 0.0, "dap": 0.0})
    q = (select(Study.modality, year, func.count(Study.id))
         .where(Study.source_kind == "openrem").group_by(Study.modality, year))
    for mod, y, n in session.execute(q):
        out[(mod, int(y))]["studies"] = n
    q = (select(Study.modality, year, func.count(Event.id),
                func.sum(case((Event.dlp.is_not(None), Event.dlp), else_=0)),
                func.sum(case((Event.dap.is_not(None), Event.dap), else_=0)))
         .join(Event, Event.study_id == Study.id)
         .where(Study.source_kind == "openrem").group_by(Study.modality, year))
    for mod, y, n, dlp, dap in session.execute(q):
        o = out[(mod, int(y))]
        o["events"], o["dlp"], o["dap"] = n, float(dlp or 0), float(dap or 0)
    return out


def reconcile(session: Session, dump_dir: Path) -> tuple[list[str], bool]:
    src, dst = _source(dump_dir), _target(session)
    lines = [f"{'modality':8} {'year':>4} {'studies src/dst':>18} {'events src/dst':>20} "
             f"{'DLP mGy·cm src/dst':>26} {'DAP Gy·cm² src/dst':>24}  status"]
    ok_all = True
    tot = defaultdict(float)
    for key in sorted(set(src) | set(dst)):
        a = src.get(key, {"studies": 0, "events": 0, "dlp": 0.0, "dap": 0.0})
        b = dst.get(key, {"studies": 0, "events": 0, "dlp": 0.0, "dap": 0.0})
        for k in ("studies", "events", "dlp", "dap"):
            tot["src_" + k] += a[k]
            tot["dst_" + k] += b[k]
        ok = (a["studies"] == b["studies"] and a["events"] == b["events"]
              and _close(a["dlp"], b["dlp"]) and _close(a["dap"], b["dap"]))
        ok_all &= ok
        lines.append(
            f"{key[0]:8} {key[1]:>4} {a['studies']:>8}/{b['studies']:<9} "
            f"{a['events']:>9}/{b['events']:<10} {a['dlp']:>12.1f}/{b['dlp']:<13.1f} "
            f"{a['dap']:>11.3f}/{b['dap']:<12.3f}  {'OK' if ok else 'DIFF'}")
    lines.append(
        f"{'TOTAL':8} {'':>4} {tot['src_studies']:>8.0f}/{tot['dst_studies']:<9.0f} "
        f"{tot['src_events']:>9.0f}/{tot['dst_events']:<10.0f} "
        f"{tot['src_dlp']:>12.1f}/{tot['dst_dlp']:<13.1f} "
        f"{tot['src_dap']:>11.3f}/{tot['dst_dap']:<12.3f}  {'OK' if ok_all else 'DIFF'}")
    return lines, ok_all


def _f(v: str) -> float:
    return float(v) if v else 0.0


def _close(a: float, b: float, rel: float = 1e-6) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-9)
