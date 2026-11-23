"""Dose statistics on the second grid (ICRP 135 / RP 185 groups, extended), next to the
national one.

Now a spec: ``seeds/specs/dosimetrix-icrp135-grid.toml`` run by the ``compare`` kind
(``specs/kinds/compare.py``): the parts compared (CT DLP and CTDIvol, radiography DAP, fluoroscopy
DAP), the RP 185 band alias, the minimum of 10 values and the 1.5x limit are its settings. This
module keeps the names the API, local DRLs, the national aggregation and the tests use, as thin
calls into the engine.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import IO

from ..specs import loader, writers
from ..specs.context import Context
from ..specs.kinds import compare as _compare

SPEC_ID = "dosimetrix-icrp135-grid"


@lru_cache(maxsize=1)
def _spec():
    return loader.parse(loader.BUNDLED / f"{SPEC_ID}.toml", bundled=True)


_CFG = _spec().body["compare"]
NATIONAL = _CFG["national_system"]
BAND_ALIAS = _CFG["band_alias"]
STATUS_RO = {**_CFG["status_ro"], None: ""}
PARTS = [(tuple(p["modalities"]), p["metric"], p["quantity"], p["unit"]) for p in _CFG["part"]]


def _edrl(session):
    """(national exam code, band key, quantity) -> RP 185 value (see compare._edrl)."""
    return _compare._edrl(session, _CFG)


def _status(median, ref, n, min_n):
    return _compare._status(median, ref, n, min_n, float(_CFG.get("above_limit", 1.5)))


def build(session, conds: list, start: dt.datetime, end: dt.datetime, min_n: int = 10,
          estimated: bool = False) -> dict:
    """``estimated=False``: body exams with a weight estimated from the growth charts go to
    "Weight unknown" instead of a weight group (measured / reported weights only)."""
    ctx = Context(session, _spec(), {"min_n": min_n, "estimated": estimated}, conds, start, end,
                  dt.datetime.now())
    return _compare.grid_report(ctx)


def write_xlsx(report: dict, target: IO, info: dict[str, object]) -> None:
    ctx = Context(None, _spec(), {}, [], None, None, dt.datetime.now())
    writers.to_xlsx(_compare.grid_result(ctx, report, info), target, ctx)
