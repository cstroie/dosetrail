"""``table``: a code system down the side, the examinations of a period counted and averaged.

This is the quarterly centralised report of Order MS 1255/2016 (INSP form), generalised so any
authority's "per procedure code: how many, by age band and sex, and the mean dose by age group"
form is a spec. For every code of a sheet's code system:

* the number of examinations in the period (an examination is a study x a code);
* the examinations by age band and sex (``count_scheme``);
* the mean (or minimum / maximum) of each quantity by age group (``mean_scheme``).

The columns follow the official workbook in order, so each numeric block pastes into it. Each row
also lists the examinations per device; a device under ``rules.min_patients`` is flagged
(``low_n``): the norm wants a mean over at least that many patients per device and examination
type. The data stays; the flag says the mean is weak, and the cells are listed on an extra sheet.

    [table]
    exclude = ["shared_administration"]          # named filters, see extract.py
    unclassified_modalities = ["DX", "CT"]       # studies of these without a code are counted
    note = "..."                                 # the last information line
    [table.values]                               # how a number is found, see extract.exam_units
    dap = { event = ["event.dap"], study = "study.total_dap" }

    [[sheet]]
    key = "ct"  title = "CT"  system = "ro-insp-1255-ct"
    count_scheme = "..."  mean_scheme = "..."  has_type = false
    [[sheet.quantity]]
    key = "dlp"  label = "DLP mediu"  unit = "mGy·cm"  digits = 1
    # optional: value = "activity", stat = "min"   (a column that summarises another value)
    # key may be omitted: a column the program does not collect (stays empty)
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import IO

from sqlalchemy import func, select

from ...db.models import Classification, Code, CodeSystem, Device, Study
from ...stats import BandLookup
from .. import extract, registry
from ..context import Context
from ..registry import kind
from ..result import Book, Result
from ..result import Sheet as OutSheet

AGG = {"mean": lambda v: sum(v) / len(v), "min": min, "max": max}


@dataclass
class Quantity:
    key: str | None  # column key; None = not collected (empty column)
    label: str
    unit: str
    digits: int = 4
    value: str | None = None  # the examination's value it summarises (default: key)
    stat: str = "mean"  # mean | min | max

    @property
    def source(self) -> str | None:
        return self.value or self.key


@dataclass
class Sheet:
    key: str
    title: str
    system: str
    count_scheme: str
    mean_scheme: str
    quantities: list[Quantity]
    has_type: bool = False  # the radiology sheet splits "Examinare" and "Tip exp."
    rows: list[dict] = field(default_factory=list)
    count_bands: list = field(default_factory=list)
    mean_bands: list = field(default_factory=list)


def sheets_of(spec) -> list[Sheet]:
    """The spec's sheets as fresh objects (a report fills ``rows`` and the bands)."""
    out = []
    for sh in spec.body["sheet"]:
        qs = [Quantity(q.get("key"), q["label"], q.get("unit", ""), q.get("digits", 4),
                       q.get("value"), q.get("stat", "mean")) for q in sh.get("quantity", [])]
        out.append(Sheet(sh["key"], sh["title"], sh["system"], sh["count_scheme"],
                         sh["mean_scheme"], qs, bool(sh.get("has_type", False))))
    return out


# ----------------------------------------------------------------------------- the report


def build_report(ctx: Context) -> dict:
    """The report as a dict: the sheets with their rows, the period, the notes and the devices."""
    spec, session = ctx.spec, ctx.session
    table = spec.body.get("table", {})
    min_patients = spec.rules.get("min_patients")
    values = table.get("values", {})
    lookups: dict[str, BandLookup] = {}

    def lk(key):
        if key not in lookups:
            lookups[key] = BandLookup(session, key, "age")
        return lookups[key]

    out_sheets: list[Sheet] = []
    notes = {"unknown_age": 0, "other_sex": 0}
    for sh in sheets_of(spec):
        codes = session.execute(select(Code.id, Code.code, Code.meaning).join(Code.system)
                                .where(CodeSystem.key == sh.system)
                                .order_by(Code.sort_order)).all()
        counts_lk, means_lk = lk(sh.count_scheme), lk(sh.mean_scheme)
        per_code: dict[int, list[dict]] = defaultdict(list)
        units = extract.exam_units(ctx, sh.system, values, table.get("exclude", ()))
        for (_sid, cid), u in units.items():
            per_code[cid].append(u)
        for c in codes:
            exam, _, kind_ = c.meaning.partition(" — ")
            row = {"code": c.code, "exam": exam if sh.has_type else c.meaning,
                   "type": (kind_ or None) if sh.has_type else None, "total": 0,
                   "devices": {}, "low_n": [],
                   "counts": {b.key: {"M": 0, "F": 0} for b in counts_lk.bands},
                   "means": {q.key: {b.key: None for b in means_lk.bands}
                             for q in sh.quantities if q.key}}
            sums = {q.source: defaultdict(list) for q in sh.quantities if q.key}
            for u in per_code.get(c.id, []):
                row["total"] += 1
                row["devices"][u["device"]] = row["devices"].get(u["device"], 0) + 1
                cb = counts_lk(u["age"])
                sex = (u["sex"] or "").upper()
                if cb is None:
                    notes["unknown_age"] += 1
                elif sex in ("M", "F"):
                    row["counts"][cb][sex] += 1
                else:
                    notes["other_sex"] += 1
                mb = means_lk(u["age"])
                if mb is None:
                    continue
                for q, by_band in sums.items():
                    if u[q] is not None:
                        by_band[mb].append(u[q])
            for q in sh.quantities:
                for b, vals in (sums[q.source].items() if q.key else ()):
                    row["means"][q.key][b] = AGG[q.stat](vals)
            if min_patients:
                row["low_n"] = sorted((d for d, n in row["devices"].items() if n < min_patients),
                                      key=lambda d: (d is None, d))
            sh.rows.append(row)
        sh.count_bands, sh.mean_bands = counts_lk.bands, means_lk.bands
        out_sheets.append(sh)

    national = (select(Classification.study_id).join(Code, Code.id == Classification.code_id)
                .join(CodeSystem).where(CodeSystem.key.in_([s.system for s in out_sheets])))
    period = []
    if ctx.start is not None:
        period.append(Study.study_datetime >= ctx.start)
    if ctx.end is not None:
        period.append(Study.study_datetime < ctx.end)
    mods = table.get("unclassified_modalities", [])
    unclassified = session.scalar(
        select(func.count()).select_from(Study)
        .where(*period, *ctx.conds, Study.modality.in_(mods), Study.id.not_in(national))) or 0
    names = dict(session.execute(select(Device.id, Device.display_name)).all())
    return {"start": ctx.start.date().isoformat() if ctx.start else None,
            "end": (ctx.end - dt.timedelta(days=1)).date().isoformat() if ctx.end else None,
            "devices": names, "sheets": out_sheets, "min_patients": min_patients,
            "notes": {**notes, "unclassified": unclassified,
                      "not_collected": table.get("not_collected", "")}}


def as_json(report: dict) -> dict:
    """The report for the Reports page: bands, quantities and the rows with their devices."""
    names = report["devices"]
    return {"period": {"from": report["start"], "to": report["end"]}, "notes": report["notes"],
            "min_patients": report["min_patients"],
            "sheets": [{"key": s.key, "title": s.title, "system": s.system,
                        "count_bands": [{"key": b.key, "label": b.label} for b in s.count_bands],
                        "mean_bands": [{"key": b.key, "label": b.label} for b in s.mean_bands],
                        "quantities": [{"key": q.key, "label": q.label, "unit": q.unit}
                                       for q in s.quantities],
                        "rows": [{**r, "devices": [{"id": d, "name": names.get(d), "n": n}
                                                   for d, n in r["devices"].items()]}
                                 for r in s.rows]} for s in report["sheets"]]}


# ----------------------------------------------------------------------------- as a Result


def to_result(ctx: Context, report: dict, info: dict | None = None) -> Result:
    spec = ctx.spec
    table = spec.body.get("table", {})
    min_patients = report["min_patients"]
    n = report["notes"]
    low_total = sum(len(r["low_n"]) for s in report["sheets"] for r in s.rows)
    lines: list[tuple[str, object]] = [
        ("Raport", spec.label("ro")), ("Perioada", f"{report['start']} – {report['end']}"),
        *(info or {}).items(),
        ("Examinări fără vârstă cunoscută", n["unknown_age"]),
        ("Examinări cu sex necunoscut / altul", n["other_sex"]),
        ("Studii fără cod național (neraportate)", n["unclassified"])]
    if min_patients:
        lines.append((f"Aparat × procedură sub {min_patients} pacienți ({_article(spec)})",
                      low_total))
    lines += [("Necolectate de Dozimetrix", n["not_collected"]), ("Notă", table.get("note", ""))]
    out: list[OutSheet] = []
    names = report["devices"]
    for s in report["sheets"]:
        head = (["Examinare", "Tip exp.", "Cod proc."] if s.has_type
                else ["Procedura", "Cod"]) + ["Nr. total exam."]
        head += [f"{b.label} {sex}" for b in s.count_bands for sex in ("M", "F")]
        for q in s.quantities:
            head += [f"{q.label} {b.label} ({q.unit})" for b in s.mean_bands]
        rows, flags = [], []
        for r in s.rows:
            cells = ([r["exam"], r["type"], r["code"]] if s.has_type
                     else [r["exam"], r["code"]]) + [r["total"] or None]
            cells += [r["counts"][b.key][sex] or None for b in s.count_bands for sex in ("M", "F")]
            for q in s.quantities:
                for b in s.mean_bands:
                    v = r["means"].get(q.key, {}).get(b.key) if q.key else None
                    cells.append(round(v, q.digits) if v is not None else None)
            rows.append(cells)
            flags.append({"low_n": [names.get(d) or "—" for d in r["low_n"]]} if r["low_n"]
                         else None)
        out.append(OutSheet(s.title, head, rows, [30] + [10] * (len(head) - 1), flags))
    if min_patients:
        low_rows = [[s.title, r["code"], r["exam"], names.get(d, "(fără aparat)"), r["devices"][d]]
                    for s in report["sheets"] for r in s.rows for d in r["low_n"]]
        out.append(OutSheet(f"Sub {min_patients} pacienti",
                            ["Foaie", "Cod", "Procedura", "Aparat", "Nr. examinări"], low_rows,
                            [22, 12, 44, 30, 14]))
    return Result([Book(f"{spec.id}.xlsx", out, spec.label("ro"), lines)],
                  extra={"report": as_json(report)}, notes=n)


def _article(spec) -> str:
    return spec.rules_meta.get("min_patients", {}).get("article", "")


class Table:
    identifying = False

    @staticmethod
    def validate(spec) -> list[str]:
        errs = []
        sheets = spec.body.get("sheet")
        if not isinstance(sheets, list) or not sheets:
            return ["table needs at least one [[sheet]]"]
        for sh in sheets:
            for k in ("key", "title", "system", "count_scheme", "mean_scheme"):
                if not sh.get(k):
                    errs.append(f"every [[sheet]] needs {k}")
            for q in sh.get("quantity", []):
                key = q.get("key") or q.get("value")
                if q.get("key") and q["key"] not in spec.body.get("table", {}).get("values", {}) \
                        and not q.get("value"):
                    errs.append(f"quantity {q['key']!r} has no entry in [table.values]")
                if key and q.get("value") and q["value"] not in spec.body["table"]["values"]:
                    errs.append(f"quantity value {q['value']!r} has no entry in [table.values]")
                if q.get("stat", "mean") not in AGG:
                    errs.append(f"quantity stat is one of {', '.join(AGG)}")
        for k, v in spec.body.get("table", {}).get("values", {}).items():
            for f in [*v.get("event", []), *([v["study"]] if v.get("study") else [])]:
                if f not in registry.FIELDS:
                    errs.append(f"[table.values.{k}] unknown field {f!r}")
        for name in spec.body.get("table", {}).get("exclude", []):
            if name.split(":")[0] not in registry.FILTERS:
                errs.append(f"unknown filter {name!r}")
        return errs

    @staticmethod
    def run(ctx: Context) -> Result:
        return to_result(ctx, build_report(ctx), {
            "Generat": ctx.now.isoformat(sep=" ", timespec="seconds"), "De": ctx.username,
            "Selecție": ctx.params.get("q") or "(toate)"})


kind("table")(Table)

__all__ = ["Table", "Quantity", "Sheet", "build_report", "as_json", "to_result", "IO"]
