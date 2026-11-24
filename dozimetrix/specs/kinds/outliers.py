"""``outliers``: who gives doses well above their colleagues' for the same exam and age group.

For optimisation and training, not blame. Every value is compared with the median of the whole
department for its national exam code and age group, which takes the case mix out. Per person
(here the DICOM Operators' Name, which many devices leave empty: the report shows how often each
room fills it):

* ``median_ratio``: median of value / group median (1.0 = like everyone else);
* ``share_p95``: share of their values above the group's 95th percentile (5 % expected).

A person is flagged with at least ``min_n`` values and a median ratio >= ``ratio_flag`` or a P95
share >= ``p95_flag`` (twice what chance gives). All three thresholds, and ``min_group`` (the values
an exam and age group need before anyone is compared with it), are rules of the spec.

    [outliers]
    system = "ro-drl-ms838-2025"       # the national exam codes the values are grouped by
    band_scheme = "ro-drl-age"
    min_group = "${rules.min_group}"   ratio_flag = "${rules.ratio_flag}"
    p95_flag = "${rules.p95_flag}"     min_n = "${rules.min_n}"
    [[outliers.source]]                # where the values come from
    modalities = ["CT", "RF", "XA"]    level = "study"
    columns = ["study.total_dlp", "study.total_dap"]   # first present, must be > 0
    [[outliers.source]]
    modalities = ["DX", "CR"]          level = "event"   columns = ["event.dap"]
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict

from sqlalchemy import func, select

from ...db.models import Classification, Code, CodeSystem, Device, Event, Study
from ...stats import BandLookup
from ..context import Context
from ..registry import kind
from ..result import Book, Result, Sheet

COLUMNS = {"study.total_dlp": Study.total_dlp, "study.total_dap": Study.total_dap,
           "study.total_rp_dose": Study.total_rp_dose, "event.dap": Event.dap,
           "event.dlp": Event.dlp, "event.ctdivol": Event.ctdivol}


def operator(name: str | None) -> str | None:
    """One spelling per person: DICOM PN carets to spaces, blanks collapsed, upper case."""
    if not name:
        return None
    s = re.sub(r"\s+", " ", name.replace("^", " ")).strip().upper()
    return s or None


def quantile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def _samples(session, conds: list, body: dict) -> list[tuple]:
    """(study id, device id, operator, exam code, age days, value)."""
    sys_id = session.scalar(select(CodeSystem.id).where(CodeSystem.key == body["system"]))
    if sys_id is None:
        return []
    cols = (Study.id, Study.device_id, Study.operator_name, Code.code, Study.age_days)
    out: list[tuple] = []
    for src in body["source"]:
        cols_ = [COLUMNS[c] for c in src["columns"]]
        value = func.coalesce(*cols_) if len(cols_) > 1 else cols_[0]  # one column: as it is
        if src["level"] == "study":
            q = (select(*cols, value)
                 .join(Classification, (Classification.study_id == Study.id)
                       & Classification.event_id.is_(None))
                 .join(Code, (Code.id == Classification.code_id) & (Code.system_id == sys_id))
                 .where(*conds, Study.modality.in_(src["modalities"]), value > 0))
        else:
            q = (select(*cols, value).join(Event, Event.study_id == Study.id)
                 .join(Classification, Classification.event_id == Event.id)
                 .join(Code, (Code.id == Classification.code_id) & (Code.system_id == sys_id))
                 .where(*conds, Study.modality.in_(src["modalities"]), value > 0))
        out += session.execute(q).all()
    return out


def build_report(ctx: Context) -> dict:
    spec, session = ctx.spec, ctx.session
    body = spec.body["outliers"]
    min_group, ratio_flag, p95_flag = body["min_group"], body["ratio_flag"], body["p95_flag"]
    min_n = int(ctx.params.get("min_n") or body["min_n"])
    band = BandLookup(session, body["band_scheme"], "age")
    names = {d.id: d.display_name or d.station_name or f"Device {d.id}"
             for d in session.scalars(select(Device))}
    meanings = {c: m for c, m in session.execute(
        select(Code.code, Code.meaning).join(CodeSystem).where(CodeSystem.key == body["system"]))}
    groups: dict[tuple, list[float]] = defaultdict(list)
    rows = []
    for sid, did, op, code, age, v in _samples(session, ctx.conds, body):
        b = band(age)
        if b is None:
            continue
        groups[(code, b)].append(v)
        rows.append((sid, did, operator(op), code, b, v))
    ref = {g: (quantile(vs, 0.5), quantile(vs, 0.95))
           for g, vs in groups.items() if len(vs) >= min_group}

    # coverage: per room, studies with and without an operator (whatever the comparison)
    cov: dict[int, dict] = defaultdict(lambda: {"studies": set(), "with": set()})
    per_op: dict[str, dict] = defaultdict(lambda: {"ratios": [], "above": 0, "studies": set(),
                                                   "devices": set(),
                                                   "exams": defaultdict(list)})
    for sid, did, op, code, b, v in rows:
        cov[did]["studies"].add(sid)
        if op:
            cov[did]["with"].add(sid)
        if not op or (code, b) not in ref:
            continue
        med, p95 = ref[(code, b)]
        o = per_op[op]
        o["ratios"].append(v / med)
        o["above"] += v > p95
        o["studies"].add(sid)
        o["devices"].add(did)
        o["exams"][(code, b)].append(v / med)

    operators = []
    for op, o in per_op.items():
        n = len(o["ratios"])
        ratio = quantile(o["ratios"], 0.5)
        share = o["above"] / n
        exams = sorted(({"exam": c, "exam_meaning": meanings.get(c), "band": band.label.get(b, b),
                         "n": len(rs), "median_ratio": quantile(rs, 0.5)}
                        for (c, b), rs in o["exams"].items()), key=lambda x: -x["n"])
        operators.append({
            "operator": op, "n": n, "studies": len(o["studies"]),
            "devices": sorted(names.get(d, str(d)) for d in o["devices"]),
            "median_ratio": ratio, "share_p95": share,
            "flag": n >= min_n and (ratio >= ratio_flag or share >= p95_flag),
            "enough": n >= min_n, "exams": exams[:8]})
    operators.sort(key=lambda x: (not x["enough"], -x["median_ratio"]))
    coverage = sorted(({"device": names.get(d, str(d)), "studies": len(c["studies"]),
                        "with_operator": len(c["with"]),
                        "share": len(c["with"]) / len(c["studies"])}
                       for d, c in cov.items()), key=lambda x: -x["studies"])
    total = sum(c["studies"] for c in coverage)
    start, end = ctx.state.get("echo") or (
        ctx.start.date().isoformat() if ctx.start else None,
        (ctx.end - dt.timedelta(days=1)).date().isoformat() if ctx.end else None)
    return {"start": start, "end": end, "min_n": min_n,
            "min_group": min_group, "ratio_flag": ratio_flag, "p95_flag": p95_flag,
            "groups_compared": len(ref), "operators": operators, "coverage": coverage,
            "studies": total,
            "studies_with_operator": sum(c["with_operator"] for c in coverage)}


def to_result(ctx: Context, rep: dict, info: dict | None = None) -> Result:
    body = ctx.spec.body["outliers"]
    lines = [("Raport", body.get("title_ro", ctx.spec.label("ro"))),
             ("Perioada", f"{rep['start']} – {rep['end']}"), *(info or {}).items(),
             ("Semnalare", f"≥ {rep['min_n']} valori și raport median ≥ "
                           f"{rep['ratio_flag']:g} sau ≥ {rep['p95_flag']:.0%} peste P95"),
             ("Scop", body.get("purpose", "")), ("Notă", body.get("note", ""))]
    ops = Sheet("Operatori", ["Operator", "Valori", "Studii", "Raport median", "Peste P95",
                              "Semnalat", "Aparate"],
                [[o["operator"], o["n"], o["studies"], round(o["median_ratio"], 3),
                  round(o["share_p95"], 3), "da" if o["flag"] else "", ", ".join(o["devices"])]
                 for o in rep["operators"]], [30, 8, 8, 12, 10, 9, 40])
    cov = Sheet("Acoperire", ["Aparat", "Studii", "Cu operator", "Pondere"],
                [[c["device"], c["studies"], c["with_operator"], round(c["share"], 3)]
                 for c in rep["coverage"]], [30, 10, 12, 10])
    return Result([Book(f"{ctx.spec.id}.xlsx", [ops, cov], ctx.spec.label("ro"), lines)],
                  extra={"report": rep})


class Outliers:
    identifying = False

    @staticmethod
    def validate(spec) -> list[str]:
        body = spec.body.get("outliers")
        if not isinstance(body, dict):
            return ["outliers needs an [outliers] table"]
        errs = [f"[outliers] needs {k}" for k in
                ("system", "band_scheme", "min_group", "ratio_flag", "p95_flag", "min_n")
                if body.get(k) in (None, "")]
        if not body.get("source"):
            errs.append("[outliers] needs at least one [[outliers.source]]")
        for src in body.get("source", []):
            if src.get("level") not in ("study", "event"):
                errs.append("a source's level is study or event")
            for c in src.get("columns", []):
                if c not in COLUMNS:
                    errs.append(f"unknown source column {c!r}; one of {', '.join(COLUMNS)}")
        return errs

    @staticmethod
    def run(ctx: Context) -> Result:
        from ..extract import window_conds

        ctx.conds = window_conds(ctx)  # the chosen period narrows the samples
        return to_result(ctx, build_report(ctx), {
            "Generat": ctx.now.isoformat(sep=" ", timespec="seconds"), "De": ctx.username,
            "Selecție": ctx.params.get("q") or "(toate)"})


kind("outliers")(Outliers)
