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


def _removed_by_year(dump_dir: Path) -> dict[int, tuple[int, float]]:
    out: dict[int, list] = defaultdict(lambda: [0, 0.0])
    path = dump_dir / "dedup-report.csv.gz"
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("year"):
                    o = out[int(r["year"])]
                    o[0] += 1
                    o[1] += _f(r["dap_gycm2"])
    return {y: (n, d) for y, (n, d) in out.items()}


def reconcile_by_year(session: Session, dump_dir: Path) -> tuple[list[str], bool]:
    """Authoritative check: per year, studies = distinct UIDs, events and DAP = OpenREM minus
    the audited duplicate exposures, DLP identical."""
    src: dict[int, dict] = {}
    with gzip.open(dump_dir / "summary_year.csv.gz", "rt", encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            src[int(r["year"])] = {"studies": int(r["uids"]), "events": int(r["events"]),
                                   "dlp": _f(r["sum_event_dlp"]),
                                   "dap": _f(r["sum_event_dap_gym2"]) * DAP_GYM2_TO_GYCM2}
    removed = _removed_by_year(dump_dir)
    year = func.coalesce(extract("year", Study.study_datetime), 0)
    dst: dict = defaultdict(lambda: {"studies": 0, "events": 0, "dlp": 0.0, "dap": 0.0})
    for y, n in session.execute(select(year, func.count(Study.id))
                                .where(Study.source_kind == "openrem").group_by(year)):
        dst[int(y)]["studies"] = n
    q = (select(year, func.count(Event.id), func.sum(func.coalesce(Event.dlp, 0)),
                func.sum(func.coalesce(Event.dap, 0)))
         .join(Event, Event.study_id == Study.id)
         .where(Study.source_kind == "openrem").group_by(year))
    for y, n, dlp, dap in session.execute(q):
        dst[int(y)].update(events=n, dlp=float(dlp or 0), dap=float(dap or 0))
    lines = [f"{'year':>4} {'studies src/dst':>17} {'events src-dup/dst':>24} "
             f"{'DLP mGy·cm src/dst':>26} {'DAP Gy·cm² src-dup/dst':>28}  status"]
    ok_all = True
    for y in sorted(set(src) | set(dst)):
        a = src.get(y, {"studies": 0, "events": 0, "dlp": 0.0, "dap": 0.0})
        b = dst.get(y, {"studies": 0, "events": 0, "dlp": 0.0, "dap": 0.0})
        n_rm, dap_rm = removed.get(y, (0, 0.0))
        exp_events, exp_dap = a["events"] - n_rm, a["dap"] - dap_rm
        ok = (a["studies"] == b["studies"] and exp_events == b["events"]
              and _close(a["dlp"], b["dlp"]) and _close(exp_dap, b["dap"], 1e-6))
        ok_all &= ok
        lines.append(f"{y:>4} {a['studies']:>8}/{b['studies']:<8} "
                     f"{a['events']:>8}-{n_rm:<6}/{b['events']:<8} "
                     f"{a['dlp']:>12.1f}/{b['dlp']:<13.1f} "
                     f"{a['dap']:>10.2f}-{dap_rm:<8.2f}/{b['dap']:<9.2f}  {'OK' if ok else 'DIFF'}")
    return lines, ok_all


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
