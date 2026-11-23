"""Dosimetrix DRL dataset (versioned) and the ICRP 135 summary.

**Dataset**: one row per study × DRL exam, the core for local DRL work and national surveys.
* The period is reduced to the month (YYYY-MM).
* Patients, devices and the facility are keyed pseudonymous codes: HMAC with the site
  secret. Repeat examinations can be counted, but codes can't be reversed without the
  secret.
* Each dose has a fixed UCUM unit. A data dictionary (``DICTIONARY``) ships with every
  file.
* Radiography rows group the exposures of one projection in a study (retakes included);
  CT rows cover the study's non-localiser series; fluoroscopy rows cover the whole study.

**ICRP 135 summary**: per room and for the facility (pooled), for each exam × patient group
× quantity of a reference set: n, median, mean, SD, P25/P75 and min/max; a flag when n is
below the minimum sample size; and the median compared with the reference value.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import IO

from sqlalchemy.orm import Session

from .. import __version__ as VERSION
from .pseudo import (  # noqa: F401  (re-exported: other modules import them from here)
    EUROPEAN,
    NATIONAL,
    NOMENCLATORS,
    _code,
    _r,
    coarse_age,
    device_code,
    patient_code,
)
from .xlsx import XlsxWriter

DATASET_VERSION = "1.0"
EXPOSURE_EXAM = ("DX", "CR", "MG", "PX")
BATCH = 2000

def _spec():
    from ..specs import loader

    return loader.parse(loader.BUNDLED / "dosimetrix-drl-dataset.toml", bundled=True)


# name, unit, description: the dictionary is the spec's column list
DICTIONARY: list[tuple[str, str, str]] = [
    (c["header"], c.get("unit", ""), c.get("doc", "")) for c in _spec().body["column"]]
FIELDS = [d[0] for d in DICTIONARY]


def dataset_rows(session: Session, conds: list, secret: bytes,
                 facility: str = "DEFAULT") -> Iterator[dict]:
    """The dataset rows as dicts keyed by field name (the engine's extractor, see
    specs/extractors.py and seeds/specs/dosimetrix-drl-dataset.toml)."""
    from ..specs.context import Context
    from ..specs.extractors import drl_dataset_rows

    ctx = Context(session, _spec(), {"facility": facility}, conds, None, None,
                  dt.datetime.now(), secret)
    for row in drl_dataset_rows(ctx):
        yield row.values


def metadata(q: str, now: dt.datetime, rows: int | None = None) -> dict:
    return {"dataset": "dosimetrix-drl", "version": DATASET_VERSION,
            "software": f"Dosimetrix {VERSION}", "generated": now.isoformat(timespec="seconds"),
            "query": q or "(all)",
            "method": "ICRP 135 / RP 185; CT per study without localisers; radiography per "
                      "projection with retakes; units UCUM; period by month; keyed pseudonyms",
            **({"rows": rows} if rows is not None else {})}


def write(fmt: str, target: IO, data: Iterator[dict], meta: dict,
          dictionary: list[tuple[str, str, str]] | None = None) -> int:
    """Write rows as json / csv / xlsx with the data dictionary and metadata."""
    dictionary = dictionary or DICTIONARY
    fields = [d[0] for d in dictionary]
    n = 0
    if fmt == "json":
        out = {**meta, "dictionary": [{"name": a, "unit": b, "description": c}
                                      for a, b, c in dictionary], "rows": []}
        for r in data:
            out["rows"].append(r)
            n += 1
        out["rows_total"] = n
        target.write(json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8"))
        return n
    if fmt == "csv":
        import csv
        import io

        text = io.TextIOWrapper(target, encoding="utf-8-sig", newline="")
        w = csv.DictWriter(text, fields, lineterminator="\r\n")
        w.writeheader()
        from .lists import safe_text

        for r in data:
            w.writerow({k: ("" if v is None else safe_text(v)) for k, v in r.items()})
            n += 1
        text.flush()
        text.detach()
        return n
    with XlsxWriter(target) as wb:
        ws = wb.sheet("Data", fields, [max(8, min(len(f) + 2, 22)) for f in fields])
        for r in data:
            ws.row([r[f] for f in fields])
            n += 1
        dic = wb.sheet("Dictionary", ["Field", "Unit", "Description"], [22, 10, 80])
        for row in dictionary:
            dic.row(list(row))
        about = wb.sheet("About", ["Item", "Value"], [16, 90])
        for k, v in {**meta, "rows": n}.items():
            about.row([k, v])
    return n


# ------------------------------------------------------------------ ICRP 135 summary

def _summary_spec():
    from ..specs import loader

    return loader.parse(loader.BUNDLED / "dosimetrix-icrp135-summary.toml", bundled=True)


SUMMARY_FIELDS = list(_summary_spec().body["compare"]["columns"])


def summary_rows(session: Session, conds: list, set_key: str, min_n: int = 20) -> list[dict]:
    """The ICRP 135 summary rows (the engine's ``compare`` kind, method drl_summary, see
    seeds/specs/dosimetrix-icrp135-summary.toml)."""
    from ..specs.context import Context
    from ..specs.kinds.compare import summary_rows as rows

    ctx = Context(session, _summary_spec(), {"set": set_key, "min_n": min_n}, conds, None, None,
                  dt.datetime.now())
    return rows(ctx)
