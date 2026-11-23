"""``records``: one row per study (more levels arrive with the specs that need them), the columns
named by field. For cohorts and extracts.

    [unit]
    level = "study"
    limit = 200000              # optional guard on the number of rows
    [[sheet]]
    name = "Studies"
    [[sheet.column]]
    header = "Date"
    field  = "study.date"
    transform = "datetime"      # optional, "name" or "name:arg"
"""

from __future__ import annotations

import datetime as dt

from .. import extract, registry
from ..context import Context
from ..registry import kind
from ..result import Book, Result, Sheet


class Records:
    identifying = False

    @staticmethod
    def validate(spec) -> list[str]:
        errs = []
        sheets = spec.body.get("sheet")
        if not isinstance(sheets, list) or not sheets:
            return ["records needs at least one [[sheet]]"]
        for sh in sheets:
            cols = sh.get("column")
            if not sh.get("name") or not isinstance(cols, list) or not cols:
                errs.append("every [[sheet]] needs a name and [[sheet.column]] entries")
                continue
            for c in cols:
                if c.get("field") not in registry.FIELDS:
                    errs.append(f"column {c.get('header')!r}: unknown field {c.get('field')!r}")
                for t in ([c["transform"]] if c.get("transform") else []):
                    if t.split(":")[0] not in registry.TRANSFORMS:
                        errs.append(f"column {c.get('header')!r}: unknown transform {t!r}")
        level = (spec.body.get("unit") or {}).get("level", "study")
        if level != "study":
            errs.append("unit.level is study (other levels come with their kinds)")
        return errs

    @staticmethod
    def run(ctx: Context) -> Result:
        sheets_spec = ctx.spec.body["sheet"]
        limit = (ctx.spec.body.get("unit") or {}).get("limit")
        rows = {sh["name"]: [] for sh in sheets_spec}
        for n, row in enumerate(extract.studies(ctx)):
            if limit and n >= limit:
                break
            for sh in sheets_spec:
                out = []
                for c in sh["column"]:
                    v = registry.FIELDS[c["field"]].fn(row)
                    if c.get("transform"):
                        v = registry.call(c["transform"], v, registry.TRANSFORMS)
                    out.append(v)
                rows[sh["name"]].append(out)
        sheets = [Sheet(sh["name"], [c["header"] for c in sh["column"]], rows[sh["name"]])
                  for sh in sheets_spec]
        title = ctx.spec.label()
        period = (f"{ctx.start:%Y-%m-%d} – {ctx.end - dt.timedelta(days=1):%Y-%m-%d}"
                  if ctx.start else "toate")
        info = [("Raport", title), ("Perioada", period),
                ("Selecție", ctx.params.get("q") or "(toate)"),
                ("Generat", ctx.now.isoformat(sep=" ", timespec="seconds")), ("De", ctx.username)]
        return Result([Book(f"{ctx.spec.id}.xlsx", sheets, title, info)])


kind("records")(Records)
