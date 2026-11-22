"""Second patient-group grid (ICRP 135 / RP 185, extended), next to the national one.

Head examinations are grouped by age (``ext-head-age``: 0–<3 months, 3 months–<1 y,
1–<6 y, 6–<18 y, ≥18 y) and body examinations by weight (``ext-body-weight``: <5, 5–<15,
15–<30, 30–<50, 50–<80, 80–<110, ≥110 kg). Bands are half-open: a patient exactly at a
limit belongs to the upper group.

Which exams are "head" is data: ``seeds/group_grid.toml`` (see its header). A body exam
of a patient without a weight goes to "Weight unknown", so the totals still match the
national grid. Weights are not estimated from age.

Group keys carry the parameter: ``age:1-6y``, ``weight:5-15kg``, ``age:?``, ``weight:?``,
``?`` (exam not classified in the chosen system).
"""

from __future__ import annotations

import csv
import tomllib
from collections import Counter, defaultdict
from functools import cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import Code, CodeSystem, GroupBand, GroupScheme, ReferenceSet, ReferenceValue
from .seeds.loader import SEEDS
from .stats import BandLookup

LABEL = "ICRP 135 / RP 185 groups (extended)"
UNKNOWN = {"age:?": "Age unknown", "weight:?": "Weight unknown", "?": "Exam not classified"}


@cache
def config() -> dict:
    with open(SEEDS / "group_grid.toml", "rb") as fh:
        return tomllib.load(fh)


def crosswalk() -> list[dict]:
    with open(SEEDS / "crosswalk.csv", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class Grid:
    def __init__(self, session: Session):
        cfg = config()
        self.age = BandLookup(session, cfg["age_scheme"], "age")
        self.weight = BandLookup(session, cfg["weight_scheme"], "weight")
        self.default = cfg.get("default", "weight")
        self.param = self._params(session, cfg)
        self.order = {**{f"age:{b.key}": i for i, b in enumerate(self.age.bands)},
                      "age:?": 50,
                      **{f"weight:{b.key}": 100 + i for i, b in enumerate(self.weight.bands)},
                      "weight:?": 150, "?": 200}
        self.labels = {**{f"age:{b.key}": b.label for b in self.age.bands},
                       **{f"weight:{b.key}": b.label for b in self.weight.bands}, **UNKNOWN}

    @staticmethod
    def _params(session: Session, cfg: dict) -> dict[tuple[str, str], str]:
        """(system key, code) -> 'age' | 'weight'."""
        out: dict[tuple[str, str], str] = {}
        rp185 = {}  # RP 185 exam -> parameter, from the scheme of its reference values
        for code, param in session.execute(
                select(Code.code, GroupScheme.parameter)
                .join(ReferenceValue, ReferenceValue.exam_code_id == Code.id)
                .join(ReferenceSet).join(GroupBand, GroupBand.id == ReferenceValue.band_id)
                .join(GroupScheme, GroupScheme.id == GroupBand.scheme_id)
                .join(CodeSystem, CodeSystem.id == Code.system_id)
                .where(CodeSystem.key == "ec-rp185")):
            rp185[code] = param
            out[("ec-rp185", code)] = param
        xw = crosswalk()
        via_rp185 = {(r["from_system"], r["from_code"]): rp185[r["to_code"]]
                     for r in xw if r["to_system"] == "ec-rp185" and r["to_code"] in rp185}
        chapters = cfg.get("chapters", {})
        default = cfg.get("default", "weight")
        for system, code, chapter in session.execute(
                select(CodeSystem.key, Code.code, Code.chapter).join(Code)
                .where(CodeSystem.key.like("ro-nom-%"))):
            out[(system, code)] = (via_rp185.get((system, code))
                                   or chapters.get((chapter or "").strip()) or default)
        # other systems inherit from the nomenclator codes mapped to them
        votes: dict[tuple[str, str], Counter] = defaultdict(Counter)
        for r in xw:
            src = out.get((r["from_system"], r["from_code"]))
            if src and r["to_system"] != "ec-rp185":
                votes[(r["to_system"], r["to_code"])][src] += 1
        for key, c in votes.items():
            out.setdefault(key, c.most_common(1)[0][0])
        for k, v in cfg.get("codes", {}).items():
            system, _, code = k.partition(":")
            out[(system, code)] = v
        return out

    def ordered_labels(self) -> list[str]:
        return [self.labels[k] for k in sorted(self.order, key=self.order.get)]

    def parameter(self, system: str | None, code: str | None) -> str | None:
        if not code:
            return None
        return self.param.get((system, code), self.default)

    def group(self, system: str | None, code: str | None, age_days, weight_kg) -> str:
        p = self.parameter(system, code)
        if p is None:
            return "?"
        if p == "age":
            b = self.age(age_days)
            return f"age:{b}" if b else "age:?"
        b = self.weight(weight_kg)
        return f"weight:{b}" if b else "weight:?"
