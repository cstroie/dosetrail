"""Applicable reference value for one CT series: by the study's exam and the patient's group.

* DLP: the national DRL (OMS 838/2025, per series) for the exam and age group; otherwise
  the European paediatric DRL (RP 185) for the exam and the patient's age (head) or weight
  (body) group.
* CTDIvol: only RP 185 defines it (head by age, body by weight).

A body exam without a weight, or an exam or age without a DRL, has no reference: it is
left out of "% of DRL" charts rather than compared with something that does not apply.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import (
    Classification,
    Code,
    CodeSystem,
    GroupBand,
    GroupScheme,
    ReferenceSet,
    ReferenceValue,
)
from .stats import BandLookup

NATIONAL, EUROPEAN = "ro-drl-ms838-2025", "ec-rp185"
SETS = {NATIONAL: "ro-ms838-2025", EUROPEAN: "ec-rp185-2018"}
LABEL = {NATIONAL: "OMS 838/2025", EUROPEAN: "RP 185"}


class CTReference:
    def __init__(self, session: Session):
        self.session = session
        # (system, exam code, quantity) -> [(scheme key | None, band key | None, value)]
        self.values: dict[tuple, list] = defaultdict(list)
        self.bands: dict[str, BandLookup] = {}
        for system, set_key in SETS.items():
            rs = session.scalar(select(ReferenceSet).filter_by(key=set_key))
            if rs is None:
                continue
            for rv, code, band, scheme in session.execute(
                    select(ReferenceValue, Code.code, GroupBand.key, GroupScheme)
                    .join(Code, Code.id == ReferenceValue.exam_code_id)
                    .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
                    .outerjoin(GroupScheme, GroupScheme.id == GroupBand.scheme_id)
                    .where(ReferenceValue.set_id == rs.id,
                           ReferenceValue.quantity.in_(["dlp", "ctdivol"]))):
                if scheme is not None and scheme.key not in self.bands:
                    self.bands[scheme.key] = BandLookup(session, scheme.key, scheme.parameter)
                self.values[(system, code, rv.quantity)].append(
                    (scheme.key if scheme else None, band, rv.value))

    def study_codes(self, study_ids: list[int]) -> dict[int, dict[str, str]]:
        out: dict[int, dict[str, str]] = defaultdict(dict)
        for i in range(0, len(study_ids), 5000):
            for sid, system, code in self.session.execute(
                    select(Classification.study_id, CodeSystem.key, Code.code)
                    .join(Code, Code.id == Classification.code_id).join(CodeSystem)
                    .where(Classification.event_id.is_(None),
                           CodeSystem.key.in_([NATIONAL, EUROPEAN]),
                           Classification.study_id.in_(study_ids[i:i + 5000]))):
                out[sid][system] = code
        return out

    def event_codes(self, event_ids: list[int]) -> dict[int, dict[str, str]]:
        """Series with codes of their own (another region than the study)."""
        out: dict[int, dict[str, str]] = defaultdict(dict)
        for i in range(0, len(event_ids), 5000):
            for eid, system, code in self.session.execute(
                    select(Classification.event_id, CodeSystem.key, Code.code)
                    .join(Code, Code.id == Classification.code_id).join(CodeSystem)
                    .where(CodeSystem.key.in_([NATIONAL, EUROPEAN]),
                           Classification.event_id.in_(event_ids[i:i + 5000]))):
                out[eid][system] = code
        return out

    def _lookup(self, system, code, quantity, age_days, weight_kg):
        for scheme, band, value in self.values.get((system, code, quantity), ()):
            if scheme is None:
                return value
            lk = self.bands[scheme]
            key = lk(age_days if lk.parameter == "age" else weight_kg)
            if key == band:
                return value
        return None

    def for_series(self, codes: dict[str, str], age_days, weight_kg) -> dict:
        """{'dlp': value, 'ctdivol': value, 'exam': code, 'source': label} (values may be None)."""
        out = {"dlp": None, "ctdivol": None, "exam": None, "source": {}}
        for quantity in ("dlp", "ctdivol"):
            for system in (NATIONAL, EUROPEAN):
                code = codes.get(system)
                if not code:
                    continue
                v = self._lookup(system, code, quantity, age_days, weight_kg)
                if v is not None:
                    out[quantity] = v
                    out["source"][quantity] = f"{LABEL[system]} {code}"
                    out["exam"] = out["exam"] or code
                    break
        return out
