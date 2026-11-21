"""Conversion of DICOM measured values to DoseTrail storage units.

Units arrive as UCUM codes, sometimes misspelled by vendors ('mGy*cm', 'dGy.cm2', 'µGy.m2',
'Gym2').
"""

from __future__ import annotations

import re

_GLUED = re.compile(r"([dcmu]?gy)((?:c|d|m)?m2|cm|mm)")

# storage unit -> {normalised source unit: factor}
_FACTORS: dict[str, dict[str, float]] = {
    "Gy.cm2": {"gy.cm2": 1, "dgy.cm2": 0.1, "cgy.cm2": 0.01, "mgy.cm2": 1e-3, "ugy.cm2": 1e-6,
               "gy.m2": 1e4, "dgy.m2": 1e3, "cgy.m2": 100, "mgy.m2": 10, "ugy.m2": 1e-2},
    "mGy.cm": {"mgy.cm": 1, "mgy.mm": 0.1, "gy.cm": 1e3, "mgy.m": 100},
    "mGy": {"mgy": 1, "gy": 1e3, "dgy": 100, "cgy": 10, "ugy": 1e-3},
    "Gy": {"gy": 1, "dgy": 0.1, "cgy": 0.01, "mgy": 1e-3, "ugy": 1e-6},
    "s": {"s": 1, "ms": 1e-3, "us": 1e-6, "min": 60},
    "mm": {"mm": 1, "cm": 10, "m": 1e3, "dm": 100},
    "mA": {"ma": 1, "ua": 1e-3, "a": 1e3},
    "mAs": {"mas": 1, "ma.s": 1, "uas": 1e-3, "ua.s": 1e-3, "as": 1e3},
    "kV": {"kv": 1, "v": 1e-3},
    "N": {"n": 1, "dan": 10, "kgf": 9.80665},
    "m2": {"m2": 1, "cm2": 1e-4, "mm2": 1e-6},
    "kg": {"kg": 1, "g": 1e-3},
    "m": {"m": 1, "cm": 0.01, "mm": 1e-3},
}


def normalise_unit(unit: str | None) -> str:
    u = (unit or "").strip().replace("µ", "u").replace("μ", "u").replace("*", ".")
    u = u.replace(" ", ".").replace("^", "").replace("[", "").replace("]", "")
    u = u.lower()
    # dose products written without a separator: 'Gym2' (Siemens Ysio), 'Gycm2', 'mGycm'
    m = _GLUED.fullmatch(u)
    return f"{m.group(1)}.{m.group(2)}" if m else u


def convert(value: float | None, unit: str | None, target: str) -> float | None:
    """Convert *value* given in *unit* to *target*; None if the unit is unknown."""
    if value is None:
        return None
    table = _FACTORS[target]
    factor = table.get(normalise_unit(unit))
    if factor is None:
        return None
    return value * factor


def known(unit: str | None, target: str) -> bool:
    return normalise_unit(unit) in _FACTORS[target]
