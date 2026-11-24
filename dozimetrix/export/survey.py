"""Anonymised export for the national paediatric dose survey (draft plan
MS-CNCAN-SDNP-2026-001, v1.0, March 2026). The layout is versioned so the plan can change.

* **Rows:** CT, one row per scan series (localisers excluded); radiography, one row per
  projection of a study (retakes summed); fluoroscopy, one row per study.
* **Patients:** paediatric only by default (age < 18 years).
* **Groups:** the RP 185 head age group and the body weight group (with 80–110 kg) are
  both given, so the analysis can pick by exam.
* **Anonymisation:** no name, national ID or birth date; the month of the exam only; age
  in days below 1 year, months up to 24 months, completed years after; keyed pseudonymous
  codes for patient, device and institution (HMAC with the site secret, keyed on stable
  identities so codes survive a re-import), so repeats can be counted centrally.
* **Validation flags** (from the plan):
  * ``check_dlp``: CTDIvol × scan length vs DLP within ±10 %;
  * ``check_phantom``: a head exam measured with the 32 cm phantom;
  * ``weight_missing``.
* **Clinical indication:** ``indication`` is the ro-nom-indications code (indications.py:
  automatic matches, mapped texts and codes set by hand; suggestions are left out);
  ``indication_text`` is the request's free text (HIS, DICOM or CSV), which can name
  people or places, so review it before sending.
* **Not collected yet:** reconstruction method and Ka,e. Their columns are present and
  empty, and the dictionary says so.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

from sqlalchemy.orm import Session


def _spec():
    from ..specs import loader

    return loader.parse(loader.BUNDLED / "ro-sdnp-paediatric-survey.toml", bundled=True)


_SPEC = _spec()
SURVEY_VERSION = _SPEC.version
EXPOSURE_EXAM = ("DX", "CR", "MG", "PX")
HEAD_CODES = {"CT-HEAD", "RX-HEAD-AP-PA", "RX-HEAD-LAT"}
BATCH = 2000

# name, unit, description: the dictionary is the spec's column list
DICTIONARY: list[tuple[str, str, str]] = [
    (c["header"], c.get("unit", ""), c.get("doc", "")) for c in _SPEC.body["column"]]
FIELDS = [d[0] for d in DICTIONARY]


def month_range(month: str) -> tuple[dt.datetime, dt.datetime]:
    """'2026-10' -> [2026-10-01, 2026-11-01)."""
    y, m = (int(x) for x in month.split("-"))
    start = dt.datetime(y, m, 1)
    end = dt.datetime(y + (m == 12), m % 12 + 1, 1)
    return start, end


def rows(session: Session, conds: list, secret: bytes, institution: str = "DEFAULT",
         max_age_years: float = 18) -> Iterator[dict]:
    """The survey rows as dicts keyed by field name (the engine's extractor, see
    specs/extractors.py and seeds/specs/ro-sdnp-paediatric-survey.toml)."""
    from ..specs.context import Context
    from ..specs.extractors import survey_rows

    ctx = Context(session, _SPEC, {"institution": institution, "max_age_years": max_age_years},
                  conds, None, None, dt.datetime.now(), secret)
    for row in survey_rows(ctx):
        yield row.values
