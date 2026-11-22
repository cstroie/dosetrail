"""National personal identification numbers: sex and birth date, offline.

When a patient ID is a valid national number, it is the most reliable source of the patient's
sex and birth date: CNP > HIS record > DICOM header. Every scheme is a small parser
registered in SCHEMES; `parse()` tries them in order and returns the first match.

Romania – CNP (Cod Numeric Personal), 13 digits:  S YY MM DD JJ NNN C
  S    sex and century: 1/2 1900–1999, 3/4 1800–1899, 5/6 2000–2099 (odd = male);
       7/8 foreign residents, 9 foreigners: century not encoded, taken as the latest one
       that puts the birth before the exam (under 100 years); 9 doesn't encode sex
  JJ   county or series code: 01–47, 51, 52, 70, 80–83
  C    check digit: sum(digit_i × "279146358279"_i) mod 11, a result of 10 counts as 1
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class NationalId:
    scheme: str
    sex: str | None  # M / F, None when the scheme doesn't encode it
    birth_date: dt.date | None  # None when the century is ambiguous


# ----------------------------------------------------------------------------- Romania

_RO_WEIGHTS = "279146358279"
_RO_COUNTIES = {f"{n:02d}" for n in range(1, 48)} | {"51", "52", "70", "80", "81", "82", "83"}
_RO_CENTURY = {"1": 1900, "2": 1900, "3": 1800, "4": 1800, "5": 2000, "6": 2000}


def ro_cnp_check_digit(first12: str) -> int:
    c = sum(int(d) * int(w) for d, w in zip(first12[:12], _RO_WEIGHTS, strict=True)) % 11
    return 1 if c == 10 else c


def _date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def ro_cnp(value: str, on: dt.date | None = None) -> NationalId | None:
    """Parse a Romanian CNP; None when it isn't one. `on` = the reference date (default today)
    used to place foreign-resident birth years in a century."""
    v = (value or "").strip()
    if len(v) != 13 or not v.isdigit() or v[0] == "0":
        return None
    if ro_cnp_check_digit(v[:12]) != int(v[12]) or v[7:9] not in _RO_COUNTIES:
        return None
    s, yy, mm, dd = v[0], int(v[1:3]), int(v[3:5]), int(v[5:7])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        return None
    sex = None if s == "9" else ("M" if int(s) % 2 else "F")
    on = on or dt.date.today()
    if s in _RO_CENTURY:
        birth = _date(_RO_CENTURY[s] + yy, mm, dd)
        if birth is None:
            return None
        return NationalId("ro_cnp", sex, birth if birth <= on else None)
    # 7/8/9: the century isn't encoded: the latest one that puts the birth on or before
    # `on` (the patient is taken to be under 100)
    birth = _date(2000 + yy, mm, dd)
    if birth is None or birth > on:
        birth = _date(1900 + yy, mm, dd)
    if birth is None:
        return None
    return NationalId("ro_cnp", sex, birth if birth <= on else None)


# ----------------------------------------------------------------------------- registry

SCHEMES: list[Callable[[str, dt.date | None], NationalId | None]] = [ro_cnp]


def parse(value: str | None, on: dt.date | None = None) -> NationalId | None:
    """The first scheme that recognises `value`, else None."""
    if not value:
        return None
    for scheme in SCHEMES:
        nid = scheme(value, on)
        if nid is not None:
            return nid
    return None
