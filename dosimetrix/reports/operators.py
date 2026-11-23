"""Operator dose report: which operators give doses well above their colleagues' for the same exam
and age group (for optimisation and training, not blame).

Now a spec: ``seeds/specs/dosimetrix-operator-doses.toml`` run by the ``outliers`` kind
(``specs/kinds/outliers.py``): the thresholds (20 values per group, ratio 1.3, 10 % over P95, 20
values per person) are its rules. This module keeps the names the API, the command line and the
tests use, as thin calls into the engine.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import IO

from ..specs import loader, writers
from ..specs.context import Context
from ..specs.kinds import outliers as _outliers
from ..specs.kinds.outliers import operator  # noqa: F401

SPEC_ID = "dosimetrix-operator-doses"


@lru_cache(maxsize=1)
def _spec():
    return loader.parse(loader.BUNDLED / f"{SPEC_ID}.toml", bundled=True)


NATIONAL = _spec().body["outliers"]["system"]
MIN_GROUP = _spec().rules["min_group"]
RATIO_FLAG, P95_FLAG = _spec().rules["ratio_flag"], _spec().rules["p95_flag"]


def build(session, conds: list, start: dt.date, end: dt.date, min_n: int = 20) -> dict:
    ctx = Context(session, _spec(), {"min_n": min_n}, conds, None, None, dt.datetime.now())
    ctx.state["echo"] = (start.isoformat(), end.isoformat())
    return _outliers.build_report(ctx)


def write_xlsx(rep: dict, target: IO, info: dict[str, object]) -> None:
    ctx = Context(None, _spec(), {}, [], None, None, dt.datetime.now())
    writers.to_xlsx(_outliers.to_result(ctx, rep, info), target, ctx)
