"""``compare``: group statistics set against a reference, two methods.

**method = "group_grid"** - dose statistics on the ICRP 135 / RP 185 grid (head exams by age, body
exams by weight; see grid.py), next to the national one. Rows: the national DRL examinations; the
patient's ICRP 135 group instead of the national age groups. Where RP 185 has a European DRL for the
exam and group the median is compared with it: below, above (up to ``above_limit``x) or well above;
fewer than ``min_n`` values is "insufficient". Studies with a DRL exclusion (weight below WHO P5 or
above P95 for age, prematurity) are left out of the statistics and counted in "excluded".

    [compare]
    method = "group_grid"
    national_system = "ro-drl-ms838-2025"   reference_set = "ec-rp185-2018"
    reference_system = "ec-rp185"           above_limit = "${rules.above_limit}"
    [compare.band_alias]                    # RP 185 band keys that differ from the extended scheme
    "6y+" = "6-18y"
    [compare.status_ro]                     # the Romanian words for the status column
    below = "sub EDRL"
    [[compare.part]]                        # what is compared: modalities, metric, RP 185 quantity
    modalities = ["CT"]  metric = "dlp"  quantity = "dlp"  unit = "mGy.cm"

**method = "drl_summary"** - the ICRP 135 summary: per room and for the facility (pooled), for each
exam x patient group x quantity of a reference set: n, median, mean, SD, P25/P75, min/max, a flag
when n is below the minimum sample size, and the median against the reference value.

    [compare]
    method = "drl_summary"
    reference_set = "ro-ms838-2025"   min_n = "${rules.min_n}"
    columns = ["unit", "device", ...]     # the output columns, in order
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ...db.models import Code, GroupBand, ReferenceSet, ReferenceValue, SizeSource, Study
from ...grid import LABEL, Grid, crosswalk
from ...stats import compute
from ..context import Context
from ..registry import kind
from ..result import Book, Result, Sheet


def _r(x, nd=6):
    return round(x, nd) if isinstance(x, float) else x


def _edrl(session, cfg: dict) -> dict[tuple[str, str, str], float]:
    """(national exam code, band key, quantity) -> RP 185 value, through the crosswalk:
    a nomenclator code (and projections) mapped both to a national and an RP 185 code."""
    rs = session.scalar(select(ReferenceSet).filter_by(key=cfg["reference_set"]))
    if rs is None:
        return {}
    from ...drl import QUANTITY

    alias = cfg.get("band_alias", {})
    national, european = cfg["national_system"], cfg["reference_system"]
    # to the local unit (Gy·cm², mGy·cm, mGy); RP 185 radiography DAP is in mGy·cm²
    values = {(c, alias.get(b, b), rv.quantity):
              rv.value / QUANTITY[rv.quantity][1].get(rv.unit, 1.0)
              for rv, c, b in session.execute(
        select(ReferenceValue, Code.code, GroupBand.key)
        .join(Code, Code.id == ReferenceValue.exam_code_id)
        .join(GroupBand, GroupBand.id == ReferenceValue.band_id)
        .where(ReferenceValue.set_id == rs.id, ReferenceValue.quantity.in_(list(QUANTITY))))}
    xw = crosswalk()
    rp185 = {(r["from_system"], r["from_code"], r["views"]): r["to_code"]
             for r in xw if r["to_system"] == european}
    nat_to_eu: dict[str, str] = {}
    for r in xw:
        if r["to_system"] == national:
            eu = rp185.get((r["from_system"], r["from_code"], r["views"]))
            if eu:
                nat_to_eu.setdefault(r["to_code"], eu)
    return {(nat, band, q): v for nat, eu in nat_to_eu.items()
            for (c, band, q), v in values.items() if c == eu}


def _status(median: float, ref: float | None, n: int, min_n: int, above: float) -> str | None:
    if ref is None:
        return None
    if n < min_n:
        return "insufficient"
    r = median / ref
    return "below" if r <= 1 else "above" if r <= above else "well_above"


def grid_report(ctx: Context) -> dict:
    """``estimated=False``: body exams with a weight estimated from the growth charts go to
    "Weight unknown" instead of a weight group (measured / reported weights only)."""
    cfg = ctx.spec.body["compare"]
    session, conds = ctx.session, ctx.conds
    national = cfg["national_system"]
    min_n = int(ctx.params.get("min_n") or cfg["min_n"])
    estimated = bool(ctx.params.get("estimated", False))
    above = float(cfg.get("above_limit", 1.5))
    grid = Grid(session)
    edrl = _edrl(session, cfg)
    period = [Study.study_datetime >= ctx.start, Study.study_datetime < ctx.end]
    rows = []
    for part in cfg["part"]:
        mods, metric, quantity, unit = (part["modalities"], part["metric"], part["quantity"],
                                        part["unit"])
        base = [*conds, *period, Study.modality.in_(mods)]
        every = compute(session, base, metric, ["exam", "ref_group"], system=national,
                        estimated_weights=estimated)
        kept = compute(session, [*base, Study.drl_exclusion.is_(None)], metric,
                       ["exam", "ref_group"], system=national, estimated_weights=estimated)
        kept_by = {(g["key"]["exam"], g["key"]["ref_group"]): g for g in kept["groups"]}
        est = compute(session, [*base, Study.drl_exclusion.is_(None),
                                Study.weight_source == SizeSource.estimated_reference],
                      metric, ["exam", "ref_group"], system=national)
        est_by = {(g["key"]["exam"], g["key"]["ref_group"]): g["n"] for g in est["groups"]} \
            if estimated else {}
        for g in every["groups"]:
            exam, group = g["key"]["exam"], g["key"]["ref_group"]
            if exam is None:
                continue
            k = kept_by.get((exam, group), {"n": 0})
            band = group.partition(":")[2]
            ref = edrl.get((exam, band, quantity))
            row = {"exam": exam, "exam_meaning": g["label"]["exam"],
                   "parameter": grid.parameter(national, exam), "group": group,
                   "group_label": g["label"]["ref_group"], "order": grid.order.get(group, 999),
                   "quantity": quantity, "metric": metric, "unit": unit,
                   "n_all": g["n"], "n": k["n"], "n_studies": k.get("n_studies", 0),
                   "excluded": g["n"] - k["n"],
                   "estimated": est_by.get((exam, group), 0) if group.startswith("weight:")
                   else 0,
                   "mean": k.get("mean"),
                   "median": k.get("median"), "p75": k.get("p75"), "edrl": ref,
                   "ratio": k["median"] / ref if ref and k.get("median") else None}
            row["status"] = _status(k["median"], ref, k["n"], min_n, above) if k["n"] else None
            rows.append(row)
    rows.sort(key=lambda r: (r["exam"], r["quantity"], r["order"]))
    n_body = sum(r["n_all"] for r in rows if r["parameter"] == "weight")
    n_body_unknown = sum(r["n_all"] for r in rows if r["group"] == "weight:?")
    n_body_est = sum(r["estimated"] for r in rows if r["parameter"] == "weight")
    start_iso = ctx.start.date().isoformat()
    end_iso = (ctx.end - dt.timedelta(days=1)).date().isoformat()
    return {"grid": LABEL, "start": start_iso, "end": end_iso, "min_n": min_n,
            "estimated_weights": estimated, "rows": rows,
            "notes": {"body_values": n_body, "body_weight_unknown": n_body_unknown,
                      "body_weight_estimated": n_body_est}}


def _round(v):
    return None if v is None else round(v, 4) if v < 10 else round(v, 1)


def grid_result(ctx: Context, report: dict, info: dict | None = None) -> Result:
    cfg = ctx.spec.body["compare"]
    status_ro = {**cfg.get("status_ro", {}), None: ""}
    n = report["notes"]
    est_line = (
        f"{n['body_weight_estimated']} din {n['body_values']} (mediana pentru vârstă și sex: "
        "OMS până la 10 ani, CDC 2000 până la 20 ani; greutăți lipsă sau în afara P1–P99)"
        if report.get("estimated_weights", True) else
        "neincluse: valorile fără greutate raportată sunt la «greutate necunoscută»")
    lines = [("Raport", cfg.get("title_ro", ctx.spec.label("ro"))),
             ("Perioada", f"{report['start']} – {report['end']}"), *(info or {}).items(),
             ("Grupe — cap", cfg.get("head_groups", "")),
             ("Grupe — corp", cfg.get("body_groups", "")),
             ("Limite", cfg.get("limits", "")),
             ("Valori corp fără greutate", f"{n['body_weight_unknown']} din {n['body_values']}"),
             ("Valori corp cu greutate estimată", est_line),
             ("Notă", cfg.get("note", "") + f" Sub {report['min_n']} valori: n insuficient.")]
    head = ["Examinare", "Cod", "Parametru", "Grupa", "Mărime", "Unitate", "n", "Studii",
            "Excluse", "Greutate estimată", "Medie", "Mediană", "P75", "EDRL RP 185",
            "Mediană/EDRL", "Stare"]
    sheets = []
    for title, param in (("Cap (varsta)", "age"), ("Corp (greutate)", "weight")):
        rows = [[r["exam_meaning"], r["exam"], "vârstă" if param == "age" else "greutate",
                 r["group_label"], r["quantity"], r["unit"], r["n"], r["n_studies"],
                 r["excluded"] or None, r["estimated"] or None, _round(r["mean"]),
                 _round(r["median"]), _round(r["p75"]), r["edrl"],
                 round(r["ratio"], 3) if r["ratio"] else None, status_ro[r["status"]]]
                for r in report["rows"] if r["parameter"] == param]
        sheets.append(Sheet(title, head, rows, [30, 18, 10, 16, 9, 9, 7, 7, 7, 9, 10, 10, 10, 10,
                                                10, 18]))
    return Result([Book(f"{ctx.spec.id}.xlsx", sheets, ctx.spec.label("ro"), lines)],
                  extra={"report": report})


# ----------------------------------------------------------------------------- ICRP 135 summary


def summary_rows(ctx: Context) -> list[dict]:
    from ...drl import compare

    cfg = ctx.spec.body["compare"]
    set_key = ctx.params.get("set") or cfg["reference_set"]
    min_n = int(ctx.params.get("min_n") or cfg["min_n"])
    out = []
    for by_device in (True, False):
        res = compare(ctx.session, ctx.conds, set_key, by_device=by_device, min_n=min_n)
        for r in res["rows"]:
            for g in r["groups"]:
                out.append({
                    "unit": "device" if by_device else "facility",
                    "device": g.get("device_name") if by_device else "(all rooms)",
                    "exam": r["exam"], "exam_name": r["exam_meaning"],
                    "group": r["band_label"] or "all", "quantity": r["quantity"],
                    "applies_to": r["applies_to"], "n": g["n"],
                    **{k: _r(g.get(k)) for k in ("median", "mean", "sd", "p25", "p75", "min",
                                                 "max")},
                    "drl": r["reference"], "drl_unit": r["unit"],
                    "median_over_drl": _r(g.get("ratio"), 3), "status": g["status"],
                    "sample_ok": g["n"] >= min_n})
    return out


def summary_result(ctx: Context, rows: list[dict]) -> Result:
    cols = ctx.spec.body["compare"]["columns"]
    sheet = Sheet(ctx.spec.body["compare"].get("sheet", "ICRP 135 summary"), cols,
                  [[r[c] for c in cols] for r in rows])
    info = [("Raport", ctx.spec.label("ro")), ("Selecție", ctx.params.get("q") or "(toate)"),
            ("Generat", ctx.now.isoformat(sep=" ", timespec="seconds")), ("De", ctx.username)]
    return Result([Book(f"{ctx.spec.id}.xlsx", [sheet], ctx.spec.label("ro"), info)])


class Compare:
    identifying = False

    @staticmethod
    def validate(spec) -> list[str]:
        cfg = spec.body.get("compare")
        if not isinstance(cfg, dict):
            return ["compare needs a [compare] table"]
        method = cfg.get("method")
        if method == "group_grid":
            errs = [f"[compare] needs {k}" for k in
                    ("national_system", "reference_set", "reference_system", "min_n")
                    if cfg.get(k) in (None, "")]
            if not cfg.get("part"):
                errs.append("group_grid needs at least one [[compare.part]]")
            for p in cfg.get("part", []):
                for k in ("modalities", "metric", "quantity", "unit"):
                    if not p.get(k):
                        errs.append(f"a [[compare.part]] needs {k}")
            if (spec.params.get("period") or {}).get("type") != "quarter":
                errs.append("group_grid needs params.period.type = quarter")
            return errs
        if method == "drl_summary":
            return [f"[compare] needs {k}" for k in ("reference_set", "min_n", "columns")
                    if not cfg.get(k)]
        return ["compare.method is group_grid or drl_summary"]

    @staticmethod
    def run(ctx: Context) -> Result:
        from ..extract import window_conds

        method = ctx.spec.body["compare"]["method"]
        if method == "drl_summary":
            ctx.conds = window_conds(ctx)
            return summary_result(ctx, summary_rows(ctx))
        return grid_result(ctx, grid_report(ctx), {
            "Generat": ctx.now.isoformat(sep=" ", timespec="seconds"), "De": ctx.username,
            "Selecție": ctx.params.get("q") or "(toate)"})


kind("compare")(Compare)
