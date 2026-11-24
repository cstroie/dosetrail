"""The annual DRL return, Order MS 1255/2016 art. 12.

Every unit sends the individual records of 20 patients examined in February and March, for every
type of examination and every X-ray or nuclear-medicine device, by 15 April. The INSP supplies one
workbook per modality family (reference/insp-forms/Art12_*.xls).

This is now a spec: ``seeds/specs/ro-insp-1255-art12.toml`` run by the ``records`` kind
(``specs/kinds/records.py``): the 20 patients, the February-March window, which national code goes
on which sheet, the columns in the official order and the 15 April deadline are all in that file,
each number with its article. This module keeps the names the API, the command line and the tests
already use, as thin calls into the engine.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from typing import IO

from ..specs import loader, writers
from ..specs.context import Context
from ..specs.fields import cnp7_of, pregnancy_text
from ..specs.kinds import records
from ..specs.result import Book, Result
from ..specs.result import Sheet as OutSheet

SPEC_ID = "ro-insp-1255-art12"


@lru_cache(maxsize=1)
def _spec():
    return loader.parse(loader.BUNDLED / f"{SPEC_ID}.toml", bundled=True)


PATIENTS_PER_SHEET = _spec().rules["patients_per_sheet"]  # art. 12(1), from the spec


@dataclass
class SheetData:
    name: str
    codes: list[str]
    columns: list[str]
    rows: list[list] = field(default_factory=list)
    patients: int = 0  # studies on the sheet (<= PATIENTS_PER_SHEET)
    available: int = 0  # studies of the window before the cut


@dataclass
class Workbook:
    family: str
    title: str
    filename: str
    device_id: int
    device_name: str
    info: dict
    sheets: list[SheetData]


def seed() -> list[dict]:
    """The families, their code systems and the code to sheet mapping (from the spec)."""
    return _spec().body["family"]


def window(year: int) -> tuple[dt.datetime, dt.datetime]:
    """February and March of the year, as [start, end)."""
    return loader.window(_spec(), {"year": year})


def cnp7(patient_id, sex, born) -> str:
    return cnp7_of(patient_id, sex, born) or ""


def _pregnancy(sex, age_days, pregnant, weeks) -> str:
    return pregnancy_text(sex, age_days, pregnant, weeks) or ""


def _context(session, year: int, conds: list | None, coefficients=None) -> Context:
    from ..api import data

    start, end = window(year)
    ctx = Context(session, _spec(), {"year": year}, conds or [], start, end, dt.datetime.now(),
                  names=data.device_names(session))
    if coefficients is not None:
        ctx.state["effdose"] = coefficients
    return ctx


def build(session, year: int, conds: list | None = None, coefficients=None) -> list[Workbook]:
    """The workbooks of the year: one per family and physical device with examinations."""
    ctx = _context(session, year, conds, coefficients)
    result = records.Records.run(ctx)
    out = []
    for b in result.books:
        sheets = [SheetData(s.name, s.meta["codes"], s.columns, s.rows, s.meta["patients"],
                            s.meta["available"]) for s in b.sheets]
        out.append(Workbook(b.meta["family"], b.title, b.filename, b.meta["device_id"],
                            b.meta["device_name"], dict(b.info), sheets))
    return out


def summary(books: list[Workbook]) -> list[dict]:
    """For the Reports page: per workbook, per sheet, patients found of the 20 asked."""
    return [{"family": b.family, "title": b.title, "device_id": b.device_id,
             "device": b.device_name, "file": b.filename,
             "sheets": [{"name": s.name, "patients": s.patients, "available": s.available,
                         "missing": max(PATIENTS_PER_SHEET - s.patients, 0)}
                        for s in b.sheets if s.available]} for b in books]


def _result(books: list[Workbook], extra: dict | None) -> Result:
    return Result([Book(b.filename, [OutSheet(s.name, s.columns, s.rows) for s in b.sheets],
                        b.title, [*b.info.items(), *(extra or {}).items()]) for b in books])


def write_workbook(book: Workbook, target: IO[bytes],
                   extra: dict[str, object] | None = None) -> None:
    ctx = Context(None, _spec(), {}, [], None, None, dt.datetime.now())
    writers.to_xlsx(_result([book], extra), target, ctx)


def write_zip(books: list[Workbook], target: IO[bytes],
              extra: dict[str, object] | None = None) -> None:
    ctx = Context(None, _spec(), {}, [], None, None, dt.datetime.now())
    writers.to_zip(_result(books, extra), target, ctx)
