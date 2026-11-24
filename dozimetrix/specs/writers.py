"""Formats a Result can be written in: xlsx, csv, json, zip (a workbook per Book)."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import IO

from ..export.xlsx import XlsxWriter
from .context import Context
from .registry import writer
from .result import Book, Result


def info_rows(ctx: Context, book: Book) -> list[tuple[str, object]]:
    """The first sheet: the book's own lines, then the edition of the spec and each rule with the
    article it comes from, so a file always says what produced its numbers."""
    s = ctx.spec
    rows = list(book.info)
    rows += [("Specificație", f"{s.id} v{s.version}"),
             ("Autoritate", f"{s.authority} {s.country}".strip()), ("Temei legal", s.legal)]
    for name, value in s.rules.items():
        meta = s.rules_meta.get(name, {})
        rows.append((f"Regulă: {name}" + (f" ({meta['article']})" if meta.get("article") else ""),
                     value))
    return [(k, json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in rows if v != ""]


def _write_book(book: Book, target: IO[bytes], ctx: Context) -> None:
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [34, 70])
        for k, v in info_rows(ctx, book):
            about.row([k, v])
        for sh in book.sheets:
            widths = sh.widths or [30] + [14] * (len(sh.columns) - 1)
            ws = wb.sheet(sh.name[:31], sh.columns, widths)
            for r in sh.rows:
                ws.row(r)


@writer("xlsx")
def to_xlsx(result: Result, target: IO[bytes], ctx: Context) -> None:
    if len(result.books) != 1:
        raise ValueError("this spec makes several workbooks: ask for zip")
    _write_book(result.books[0], target, ctx)


@writer("zip")
def to_zip(result: Result, target: IO[bytes], ctx: Context) -> None:
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for b in result.books:
            buf = io.BytesIO()
            _write_book(b, buf, ctx)
            z.writestr(b.filename, buf.getvalue())


def _csv_bytes(columns: list[str], rows: list[list]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(columns)
    w.writerows(rows)
    return out.getvalue().encode("utf-8-sig")


@writer("csv")
def to_csv(result: Result, target: IO[bytes], ctx: Context) -> None:
    """The first sheet of a workbook (or the one named by the ``sheet`` parameter): the data, not
    the dictionary. Several workbooks give a zip of one csv each."""
    want = ctx.params.get("sheet")

    def pick(book: Book):
        return next((s for s in book.sheets if s.name == want), book.sheets[0])

    if len(result.books) == 1:
        s = pick(result.books[0])
        target.write(_csv_bytes(s.columns, s.rows))
        return
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for b in result.books:
            s = pick(b)
            z.writestr(b.filename.rsplit(".", 1)[0] + ".csv", _csv_bytes(s.columns, s.rows))


def as_json(result: Result, ctx: Context) -> dict:
    return {"spec": ctx.spec.public(), "notes": result.notes, **result.extra,
            "summary": result.summary,
            "books": [{"filename": b.filename, "title": b.title, "info": info_rows(ctx, b),
                       "sheets": [{"name": s.name, "columns": s.columns, "rows": s.rows,
                                   "flags": s.flags} for s in b.sheets]} for b in result.books]}


@writer("json")
def to_json(result: Result, target: IO[bytes], ctx: Context) -> None:
    target.write(json.dumps(as_json(result, ctx), ensure_ascii=False, default=str).encode("utf-8"))
