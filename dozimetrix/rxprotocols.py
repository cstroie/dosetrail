"""Radiography protocol names -> anatomy, projection and the size band the name states.

The Siemens Ysio protocols are named ``<code> <anatomy> <projection> <band>``:
``W035 Torace a.p. 5-15Kg``, ``W001a Craniu a.p. 1-6 ani``, ``X070 Mana d.v./v.d. >50 Kg``,
``W008 Sinusuri`` (no projection, no band). The code (W035, T035, X035, OT_, OW_) says which
program set it comes from, not what was imaged, so it is kept apart (``prefix``) and the
anatomy merges the sets.

The band is what the operator picked, in the name; it is not derived from the patient. It
is a weight in kg (``5-15Kg``, ``<5 Kg``, ``>50 kg``, ``2500g-3000g``) or an age in months
(``1-6 ani`` = 12-72 months, ``peste 6 ani``, ``3-12 luni``, ``11-15 Years``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .classify import normalise

INF = float("inf")

_PREFIX = re.compile(r"^\s*(?:[a-z]{1,2}\d{3}(?:[a-z](?=[^a-z]|$))?|o[tw]_|[a-z](?= [a-z]{3}))\s*",
                     re.IGNORECASE)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")

# On the normalised text (lower case, no accents, "a.p." -> "a p", "5-15Kg" -> "5 15kg").
_AGE = re.compile(r"\b(?:(\d+) (\d+)|(peste|sub) (\d+)) ?(ani|an|ans|years?|y|luni|months?)\b")
_KG = r"(?:kg|kgs)"
_WT_RANGE = re.compile(rf"\b(?:c )?(\d+) ?(kg|g)? ?(\d+) ?({_KG}|g)\b")
_WT_OPEN = re.compile(rf"\b(peste|sub) ?(\d+) ?({_KG}|g)?\b")
_WT_BARE = re.compile(r"\b(\d{1,3}) (\d{1,3})\s*$")  # "Femur lat. 15-30": no unit, a weight

_PROJ_WORDS = {"ap", "pa", "lat", "lateral", "oblic", "obl", "dv", "vd", "dp", "erect", "profil"}
_PROJ_PAIR = {("a", "p"), ("p", "a"), ("d", "v"), ("v", "d"), ("d", "p")}
_ALIAS = {"chest": "torace", "foot": "picior", "hand": "mana", "knee": "genunchi",
          "skull": "craniu", "spine": "coloana"}


@dataclass(frozen=True)
class RxName:
    prefix: str          # "W035", "OT_": the program set, kept for the tooltip
    anatomy: str         # "Torace", "Col Cervicala", "Craniu"
    projection: str      # "AP", "PA", "AP/PA", "LAT", "OBL", "DV", "DP", "" when unstated
    kind: str            # "weight" | "age" | "none"
    lo: float | None     # kg or months; None for kind "none"
    hi: float | None     # INF for an open upper end (">50 Kg")
    label: str           # "5-15 kg", "<5 kg", ">6 y", "3-12 mo", "" when no band


def _num(x: float) -> str:
    return f"{x:g}"


def _age(m: re.Match) -> tuple[float, float, str] | None:
    unit = m[5]
    mul = 1.0 if unit in ("luni", "month", "months") else 12.0
    suf = "mo" if mul == 1.0 else "y"
    if m[1]:
        lo, hi = float(m[1]), float(m[2])
        return lo * mul, hi * mul, f"{_num(lo)}-{_num(hi)} {suf}"
    if m[3]:
        n = float(m[4])
        return (n * mul, INF, f">{_num(n)} {suf}") if m[3] == "peste" else (0.0, n * mul, f"<{_num(n)} {suf}")
    return None


def _weight(t: str) -> tuple[float, float, str, tuple[int, int]] | None:
    """(lo, hi, label, span) of a weight band in the normalised text."""
    if m := _WT_OPEN.search(t):
        n = float(m[2]) / (1000 if m[3] == "g" else 1)  # no unit: kg
        lo, hi = (n, INF) if m[1] == "peste" else (0.0, n)
        return lo, hi, (f">{_num(n)} kg" if m[1] == "peste" else f"<{_num(n)} kg"), m.span()
    if m := _WT_RANGE.search(t):
        scale = 1000 if m[4] == "g" else 1
        a, b = float(m[1]) / scale, float(m[3]) / scale
        if a <= b:
            return a, b, f"{_num(a)}-{_num(b)} kg", m.span()
    if m := _WT_BARE.search(t):
        a, b = float(m[1]), float(m[2])
        if a < b:
            return a, b, f"{_num(a)}-{_num(b)} kg", m.span()
    return None


@lru_cache(maxsize=8192)  # a unit has a few hundred distinct names; RxName is frozen
def parse(protocol: str | None) -> RxName | None:
    """The parts of one radiography protocol name, or None for an empty name."""
    raw = (protocol or "").strip()
    if not raw:
        return None
    m = _PREFIX.match(raw)
    prefix = m.group(0).strip().upper() if m else ""
    rest = raw[m.end():] if m else raw
    rest = _CAMEL.sub(" ", rest).replace(">", " peste ").replace("<", " sub ").replace("≥", " peste ")
    t = normalise(rest)
    kind, lo, hi, label = "none", None, None, ""
    if a := _AGE.search(t):
        got = _age(a)
        if got:
            kind, (lo, hi, label) = "age", got
            t = t[:a.start()] + " " + t[a.end():]
    if kind == "none":
        w = _weight(t)
        if w:
            kind, lo, hi, label = "weight", w[0], w[1], w[2]
            t = t[:w[3][0]] + " " + t[w[3][1]:]
    tokens = [x for x in t.split() if x not in ("sub", "peste")]
    cut = len(tokens)
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok in _PROJ_WORDS or (tok, nxt) in _PROJ_PAIR:
            cut = i
            break
    anatomy_tokens = [_ALIAS.get(x, x) for x in tokens[:cut]]
    rest_tokens = tokens[cut:]
    anatomy = " ".join(w.capitalize() for w in anatomy_tokens)
    if not anatomy:
        return None
    return RxName(prefix, anatomy, _projection(rest_tokens), kind, lo, hi, label)


def _projection(tokens: list[str]) -> str:
    s = " " + " ".join(tokens) + " "
    ap, pa = " a p " in s or " ap " in s, " p a " in s or " pa " in s
    if ap and pa:
        return "AP/PA"
    if " d v " in s or " v d " in s or " dv " in s or " vd " in s:
        return "DV"
    if ap:
        return "AP"
    if pa:
        return "PA"
    if " d p " in s or " dp " in s:
        return "DP"
    if " lat " in s or " lateral " in s or " profil " in s:
        return "LAT"
    if " obl " in s or " oblic " in s:
        return "OBL"
    if " erect " in s:
        return "ERECT"  # the patient's position, not a view: "Abdomen erect. pos. LSL"
    return ""


def band_sort_key(kind: str, lo: float | None, hi: float | None) -> tuple:
    """Order bands on an axis: the ones with a number by lower then upper end, the unstated last."""
    if kind == "none" or lo is None:
        return (1, 0.0, 0.0)
    return (0, lo, hi if hi is not None else INF)
