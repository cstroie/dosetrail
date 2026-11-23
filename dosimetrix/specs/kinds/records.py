"""``records``: individual records, a row per study or per examination, columns named by field.

For cohorts, extracts and the forms an authority asks for as "N patients per examination".

**Studies** (the default): one sheet per ``[[sheet]]``, one row per study of the query.

    [unit]
    level = "study"
    limit = 200000              # optional guard on the number of rows
    [[sheet]]
    name = "Studies"
    [[sheet.column]]
    header = "Date"
    field  = "study.date"
    transform = "datetime"      # optional, "name" or "name:arg"

**Extractor** (``level = "extract"``): the rows come from a registered extractor (a row shape no
column list can express: a CT series per row, exposures grouped per projection) and the spec lists
the columns by ``value`` key with their ``unit`` and ``doc``; ``[dictionary] sheet = "..."`` adds
the data dictionary as a sheet.

    [unit]
    level = "extract"
    extractor = "survey_rows"
    [dictionary]
    sheet = "Dictionary"
    [[column]]
    header = "dlp"  value = "dlp"  unit = "mGy.cm"  doc = "DLP of the series"

**Examinations** (``level = "exam"``): families of workbooks; a family is a code system whose codes
are sorted onto sheets. One workbook per family and physical device, and per sheet the first
``select.first`` studies of the window in time order (deterministic, so a rerun gives the same
patients). A column is a ``field``, a ``value`` summed from the exam's events (``[family.values]``,
see extract.exam_units) or ``seq`` (the row number).

    [unit]
    level = "exam"
    none_as = ""                # what an empty cell shows
    [select]
    first = 20
    [window]
    months = [2, 3]             # only these months of the chosen year
    [[book.info]]               # the workbook's first sheet: a field of the device, or a text
    header = "Anul"
    text = "{year}"             # {year} {start} {end} {first}
    [[family]]
    key = "ct"  title = "CT"  file = "Art12_CT_DLP"  system = "ro-insp-1255-ct"
    modalities = ["CT"]  per = "study"        # per = "event": a row per event (a mammography view)
    exclude = ["shared_administration"]
    [family.values]
    dap = { event = ["event.dap"], study = "study.total_dap" }
    [[family.column]]
    header = "DAP"  value = "dap"  transform = "round:4"
    [[family.sheet]]
    name = "Cap"
    codes = ["C", "CSC"]
"""

from __future__ import annotations

import datetime as dt

from .. import extract, registry
from ..context import Context
from ..extract import Row
from ..registry import kind
from ..result import Book, Result, Sheet


def _cell(c: dict, row: Row, n: int | None = None):
    if c.get("seq"):
        return n
    v = row.values.get(c["value"]) if c.get("value") else registry.FIELDS[c["field"]].fn(row)
    if c.get("transform"):
        v = registry.call(c["transform"], v, registry.TRANSFORMS)
    return v


def _check_columns(cols, errs, where):
    for c in cols:
        if c.get("field") is not None and c["field"] not in registry.FIELDS:
            errs.append(f"{where} column {c.get('header')!r}: unknown field {c.get('field')!r}")
        if not (c.get("field") or c.get("value") or c.get("seq")):
            errs.append(f"{where} column {c.get('header')!r} needs field, value or seq")
        if c.get("transform") and c["transform"].split(":")[0] not in registry.TRANSFORMS:
            errs.append(f"{where} column {c.get('header')!r}: unknown transform {c['transform']!r}")


class Records:
    identifying = False

    @staticmethod
    def validate(spec) -> list[str]:
        level = (spec.body.get("unit") or {}).get("level", "study")
        errs: list[str] = []
        if level == "study":
            sheets = spec.body.get("sheet")
            if not isinstance(sheets, list) or not sheets:
                return ["records needs at least one [[sheet]]"]
            for sh in sheets:
                cols = sh.get("column")
                if not sh.get("name") or not isinstance(cols, list) or not cols:
                    errs.append("every [[sheet]] needs a name and [[sheet.column]] entries")
                    continue
                _check_columns(cols, errs, f"sheet {sh['name']}:")
            return errs
        if level == "extract":
            name = (spec.body.get("unit") or {}).get("extractor")
            if name not in registry.EXTRACTORS:
                errs.append(f"unknown extractor {name!r}; one of {', '.join(registry.EXTRACTORS)}")
            cols = spec.body.get("column")
            if not isinstance(cols, list) or not cols:
                return [*errs, "an extractor spec needs [[column]] entries"]
            for c in cols:
                if not c.get("header") or not (c.get("value") or c.get("field")):
                    errs.append(f"column {c.get('header')!r} needs a header and a value")
            _check_columns([c for c in cols if c.get("field")], errs, "")
            return errs
        if level != "exam":
            return ["unit.level is study, exam or extract"]
        fams = spec.body.get("family")
        if not isinstance(fams, list) or not fams:
            return ["level = exam needs at least one [[family]]"]
        for f in fams:
            for k in ("key", "title", "file", "system", "modalities"):
                if not f.get(k):
                    errs.append(f"every [[family]] needs {k}")
            if f.get("per", "study") not in ("study", "event"):
                errs.append("family.per is study or event")
            if not f.get("sheet") or not f.get("column"):
                errs.append(f"family {f.get('key')}: needs [[family.sheet]] and [[family.column]]")
                continue
            _check_columns(f["column"], errs, f"family {f['key']}:")
            for k, v in f.get("values", {}).items():
                for name in [*v.get("event", []), *([v["study"]] if v.get("study") else [])]:
                    if name not in registry.FIELDS:
                        errs.append(f"family {f['key']} values.{k}: unknown field {name!r}")
            for name in f.get("exclude", []):
                if name.split(":")[0] not in registry.FILTERS:
                    errs.append(f"family {f['key']}: unknown filter {name!r}")
        for f in fams:
            for c in f.get("column", []):
                if c.get("value") and c["value"] not in f.get("values", {}):
                    errs.append(f"family {f['key']}: value {c['value']!r} is not in "
                                "[family.values]")
        return errs

    # ------------------------------------------------------------------ run
    @staticmethod
    def run(ctx: Context) -> Result:
        level = (ctx.spec.body.get("unit") or {}).get("level", "study")
        return {"exam": _run_exam, "extract": _run_extract}.get(level, _run_study)(ctx)


def _period(ctx: Context) -> str:
    return (f"{ctx.start:%Y-%m-%d} – {ctx.end - dt.timedelta(days=1):%Y-%m-%d}"
            if ctx.start else "toate")


def _run_study(ctx: Context) -> Result:
    sheets_spec = ctx.spec.body["sheet"]
    limit = (ctx.spec.body.get("unit") or {}).get("limit")
    rows = {sh["name"]: [] for sh in sheets_spec}
    for n, row in enumerate(extract.studies(ctx)):
        if limit and n >= limit:
            break
        for sh in sheets_spec:
            rows[sh["name"]].append([_cell(c, row) for c in sh["column"]])
    sheets = [Sheet(sh["name"], [c["header"] for c in sh["column"]], rows[sh["name"]])
              for sh in sheets_spec]
    title = ctx.spec.label()
    info = [("Raport", title), ("Perioada", _period(ctx)),
            ("Selecție", ctx.params.get("q") or "(toate)"),
            ("Generat", ctx.now.isoformat(sep=" ", timespec="seconds")), ("De", ctx.username)]
    return Result([Book(f"{ctx.spec.id}.xlsx", sheets, title, info)])


def _run_extract(ctx: Context) -> Result:
    body = ctx.spec.body
    cols = body["column"]
    limit = (body.get("unit") or {}).get("limit")
    rows = []
    for n, row in enumerate(registry.EXTRACTORS[body["unit"]["extractor"]](ctx)):
        if limit and n >= limit:
            break
        rows.append([_cell(c, row) for c in cols])
    sheets = [Sheet(body["unit"].get("sheet", "Data"), [c["header"] for c in cols], rows,
                    [max(8, min(len(c["header"]) + 2, 22)) for c in cols])]
    dic = body.get("dictionary")
    if dic:
        sheets.append(Sheet(dic.get("sheet", "Dictionary"), ["Field", "Unit", "Description"],
                            [[c["header"], c.get("unit", ""), c.get("doc", "")] for c in cols],
                            [22, 10, 80]))
    title = ctx.spec.label()
    info = [("Raport", title), ("Perioada", _period(ctx)),
            ("Selecție", ctx.params.get("q") or "(toate)"), ("Rânduri", len(rows)),
            ("Generat", ctx.now.isoformat(sep=" ", timespec="seconds")), ("De", ctx.username)]
    return Result([Book(f"{ctx.spec.id}.xlsx", sheets, title, info)])


def _run_exam(ctx: Context) -> Result:
    from sqlalchemy import select

    from ...db.models import Device

    body, session = ctx.spec.body, ctx.session
    none_as = (body.get("unit") or {}).get("none_as")
    first = (body.get("select") or {}).get("first")
    devs = {d.id: d for d in session.scalars(select(Device))}

    def physical(i):
        d = devs.get(i)
        return devs.get(d.merged_into_id, d) if d is not None and d.merged_into_id else d

    subst = {"year": ctx.params.get("year", ""), "first": first or "",
             "start": f"{ctx.start:%Y-%m-%d}" if ctx.start else "",
             "end": f"{ctx.end - dt.timedelta(days=1):%Y-%m-%d}" if ctx.end else ""}
    books: list[Book] = []
    summary: list[dict] = []
    for fam in body["family"]:
        by_code = {c: s["name"] for s in fam["sheet"] for c in s["codes"]}
        rows = extract.exam_rows(ctx, fam["system"], fam["modalities"], fam.get("values", {}),
                                 fam.get("per", "study") == "event", fam.get("exclude", ()))
        per_dev: dict[int, dict[str, list[Row]]] = {}
        for row in rows.values():
            sheet, dev = by_code.get(row.code.code), physical(row.s.device_id)
            if sheet is None or dev is None:
                continue
            per_dev.setdefault(dev.id, {}).setdefault(sheet, []).append(row)
        headers = [c["header"] for c in fam["column"]]
        for dev_id, by_sheet in sorted(per_dev.items()):
            dev = devs[dev_id]
            sheets = []
            for sh in fam["sheet"]:
                units = sorted(by_sheet.get(sh["name"], []),
                               key=lambda r: (r.s.study_datetime, r.s.id, r.code.code))
                studies = list(dict.fromkeys(r.s.id for r in units))
                keep = set(studies[:first] if first else studies)
                chosen = [r for r in units if r.s.id in keep]
                out = []
                for i, r in enumerate(chosen, 1):
                    cells = [_cell(c, r, i) for c in fam["column"]]
                    out.append([none_as if v is None and none_as is not None else v
                                for v in cells])
                sheets.append(Sheet(sh["name"], headers, out, meta={
                    "codes": sh["codes"], "patients": len(keep), "available": len(studies)}))
            drow = Row(ctx, _blank_study(), None, dev)
            info = []
            for item in body.get("book", {}).get("info", []):
                v = item["text"].format(**subst) if "text" in item \
                    else registry.FIELDS[item["field"]].fn(drow)
                info.append((item["header"], none_as if v is None and none_as is not None else v))
            name = dev.display_name or dev.station_name or f"Device {dev.id}"
            safe = "".join(c if c.isalnum() else "_" for c in name).strip("_") or "device"
            fname = f"{fam['file']}_{safe}_{dev.id}.xlsx"
            books.append(Book(fname, sheets, fam["title"], info,
                              {"family": fam["key"], "device_id": dev.id, "device_name": name}))
            summary.append({"family": fam["key"], "title": fam["title"], "device_id": dev.id,
                            "device": name, "file": fname,
                            "sheets": [{"name": s.name, "patients": s.meta["patients"],
                                        "available": s.meta["available"],
                                        "missing": max((first or 0) - s.meta["patients"], 0)}
                                       for s in sheets if s.meta["available"]]})
    return Result(books, extra={"per_sheet": first, "window": [subst["start"], subst["end"]]},
                  summary=summary)


def _blank_study():
    from ...db.models import Study

    return Study()


kind("records")(Records)
