"""Age arithmetic and patient-group assignment.

Ages are computed with calendar arithmetic (completed months/years), never days/365.25.
Bands are half-open: [lower, upper). A patient exactly on a limit belongs to the upper band.

Month-end rule: if the birth day does not exist in the current month (e.g. born on the 31st,
or on 29 February), the month/year is completed on the last day of that month.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass


def completed_months(birth: dt.date, on: dt.date) -> int:
    """Whole calendar months completed between *birth* and *on*."""
    months = (on.year - birth.year) * 12 + (on.month - birth.month)
    if on.day < birth.day:
        # not yet reached the "monthiversary" – unless birth day doesn't exist in this month
        last_day = _last_day_of_month(on)
        if not (on.day == last_day and birth.day > last_day):
            months -= 1
    return max(months, 0)


def completed_years(birth: dt.date, on: dt.date) -> int:
    return completed_months(birth, on) // 12


def age_days(birth: dt.date, on: dt.date) -> int:
    return (on - birth).days


def _last_day_of_month(d: dt.date) -> int:
    nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return (nxt - dt.timedelta(days=1)).day


_DICOM_AGE = re.compile(r"^(\d{3})([DWMY])$")


def parse_dicom_age(value: str | None) -> tuple[int, str] | None:
    """Parse DICOM AS (e.g. '005Y', '011M', '003W', '010D') -> (amount, unit)."""
    if not value:
        return None
    m = _DICOM_AGE.match(value.strip().upper())
    if not m:
        return None
    return int(m.group(1)), m.group(2)


@dataclass(frozen=True)
class AgeSpec:
    """An age expressed in a unit ('d', 'mo', 'y') – used for band limits."""

    amount: float
    unit: str


def reached(birth: dt.date, on: dt.date, limit: AgeSpec) -> bool:
    """True if the patient (born *birth*) has reached age *limit* on date *on*."""
    if limit.unit == "d":
        return age_days(birth, on) >= limit.amount
    if limit.unit == "mo":
        return completed_months(birth, on) >= limit.amount
    if limit.unit == "y":
        return completed_months(birth, on) >= limit.amount * 12
    raise ValueError(f"unknown age unit {limit.unit!r}")


def reached_by_days(days: int, limit: AgeSpec) -> bool:
    """Fallback when only an age in days is known (no birth date): approximate months/years."""
    if limit.unit == "d":
        return days >= limit.amount
    if limit.unit == "mo":
        return days >= limit.amount * 30.4375
    if limit.unit == "y":
        return days >= limit.amount * 365.25
    raise ValueError(f"unknown age unit {limit.unit!r}")


@dataclass(frozen=True)
class Band:
    key: str
    label: str
    lower: float | None
    lower_unit: str | None
    upper: float | None
    upper_unit: str | None


def band_for_age(bands: list[Band], *, birth: dt.date | None = None, on: dt.date | None = None,
                 days: int | None = None) -> Band | None:
    """Pick the band containing the age. Prefer exact birth date; else age in days."""
    for b in bands:
        lo_ok = b.lower is None or _reached(AgeSpec(b.lower, b.lower_unit or "y"), birth, on, days)
        hi_ok = b.upper is None or not _reached(
            AgeSpec(b.upper, b.upper_unit or "y"), birth, on, days
        )
        if lo_ok and hi_ok:
            return b
    return None


def _reached(spec: AgeSpec, birth, on, days) -> bool:
    if birth is not None and on is not None:
        return reached(birth, on, spec)
    if days is not None:
        return reached_by_days(days, spec)
    raise ValueError("need birth+on or days")


def band_for_weight(bands: list[Band], weight_kg: float | None) -> Band | None:
    if weight_kg is None:
        return None
    for b in bands:
        if (b.lower is None or weight_kg >= b.lower) and (b.upper is None or weight_kg < b.upper):
            return b
    return None


def format_age(days: int | None) -> str:
    """Display convention: days below 1 month, months up to 24 months, then years."""
    if days is None:
        return "–"
    if days < 31:
        return f"{days} d"
    if days < 730:
        return f"{int(days // 30.4375)} mo"
    return f"{int(days // 365.25)} y"
