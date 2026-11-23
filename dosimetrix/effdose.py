"""Estimated effective dose of a nuclear-medicine examination (Order MS 1255/2016 art. 3(2) B e):
administered activity x a coefficient in mSv/MBq.

The coefficients are **not shipped**: they depend on the radiopharmaceutical, the age and the
source (ICRP 128 / 106, the EANM paediatric dosage card). The medical physicist enters them with
``dosimetrix effective-dose set`` (nuclide, form, age_to_years, msv_per_mbq, source), and the
Art. 12 workbooks fill the "Doza efectiva estimata" column only where a coefficient applies.

For a patient the row used is the one with the smallest ``age_to_years`` at or above the age (a
row without an age limit covers adults); a nuclide and form without a row stay empty.
"""

from __future__ import annotations


class Coefficients:
    def __init__(self, rows: list[tuple[str, str, float | None, float]]):
        self._by: dict[tuple[str, str], list[tuple[float, float]]] = {}
        for nuclide, form, age_to, msv in rows:
            key = (nuclide.strip().lower(), form.strip().lower())
            self._by.setdefault(key, []).append((float("inf") if age_to is None else age_to, msv))
        for pts in self._by.values():
            pts.sort()

    def __bool__(self) -> bool:
        return bool(self._by)

    def __call__(self, nuclide: str, form: str, age_days: int | None) -> float | None:
        pts = self._by.get((nuclide.strip().lower(), form.strip().lower()))
        if not pts:
            return None
        years = age_days / 365.25 if age_days is not None else float("inf")
        for limit, msv in pts:
            if years <= limit:
                return msv
        return None


def load(session) -> Coefficients:
    from sqlalchemy import select

    from .db.models import EffectiveDoseCoefficient as C

    return Coefficients([(c.nuclide, c.form, c.age_to_years, c.msv_per_mbq)
                         for c in session.scalars(select(C))])
