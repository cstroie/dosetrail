"""DRL trend over the last quarters (Order MS 1245/2021 art. 8(2)).

Now a spec: ``seeds/specs/ro-ms1245-drl-trend.toml`` run by the ``series`` kind
(``specs/kinds/series.py``); the number of quarters is its rule. This module keeps the names the
API, the command line and the tests use, as thin calls into the engine.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import IO

from ..specs import loader, writers
from ..specs.context import Context
from ..specs.kinds import series as _series
from ..specs.kinds.series import default_end, quarters_back  # noqa: F401

SPEC_ID = "ro-ms1245-drl-trend"


@lru_cache(maxsize=1)
def _spec():
    return loader.parse(loader.BUNDLED / f"{SPEC_ID}.toml", bundled=True)


def _slope(points):  # kept for callers that used it
    return _series.slope(points)


def build(session, conds: list, year: int, quarter: int, n: int = 6,
          set_key: str = "ro-ms838-2025") -> dict:
    ctx = Context(session, _spec(), {"year": year, "quarter": quarter, "quarters": n,
                                     "set": set_key}, conds, None, None, dt.datetime.now())
    return _series.build_report(ctx)


def write_xlsx(report: dict, target: IO, info: dict[str, object]) -> None:
    ctx = Context(None, _spec(), {}, [], None, None, dt.datetime.now())
    writers.to_xlsx(_series.to_result(ctx, report, info), target, ctx)
