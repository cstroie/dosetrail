"""Quarterly INSP report, Order MS 1255/2016 (annex 4).

The report is now a spec: ``seeds/specs/ro-insp-1255-quarterly.toml`` run by the ``table`` kind
(``specs/kinds/table.py``). The order's numbers (the 30 patients per device, the 20th of the month
after the quarter) are its ``[rules]``, each with the article it comes from. This module keeps the
names the API, the command line and the other reports already use, as thin calls into the engine.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import IO

from ..specs import loader, writers
from ..specs.context import Context
from ..specs.extract import shared_administration as _shared_administration  # noqa: F401
from ..specs.kinds import table as _table
from ..specs.kinds.table import Quantity, Sheet  # noqa: F401

SPEC_ID = "ro-insp-1255-quarterly"


@lru_cache(maxsize=1)
def _spec():
    return loader.parse(loader.BUNDLED / f"{SPEC_ID}.toml", bundled=True)


MIN_PATIENTS = _spec().rules["min_patients"]  # art. 7(3), from the spec


def sheets() -> list[Sheet]:
    return _table.sheets_of(_spec())


SHEETS = sheets()  # for callers that only need the systems / titles


def quarter_range(year: int, quarter: int) -> tuple[dt.datetime, dt.datetime]:
    return loader._quarter(year, quarter)  # a SpecError (a ValueError) outside 1..4


def build(session, conds: list, start: dt.datetime, end: dt.datetime) -> dict:
    ctx = Context(session, _spec(), {}, conds, start, end, dt.datetime.now())
    return _table.build_report(ctx)


def as_json(report: dict) -> dict:
    return _table.as_json(report)


def write_xlsx(report: dict, target: IO[bytes], info: dict[str, object]) -> None:
    ctx = Context(None, _spec(), {}, [], None, None, dt.datetime.now())
    writers.to_xlsx(_table.to_result(ctx, report, info), target, ctx)
