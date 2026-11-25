"""One vocabulary for radiography exposures across devices: anatomy, projection, size band.

Devices name the same exam differently ("W001a Craniu a.p. 1-6 ani", "T001a Skull a.p. 1-6
ani", "CRANIU", "X035 Pulmonara 0-5 kg"), so a chart that groups by the raw protocol name
cannot compare them. What is shared is already in the database:

* **anatomy**: the exposure's own exam code in the local nomenclator (``ro-nom-rx``, from
  the rules in ``seeds/classify_rules.toml`` and the human decisions of the Exam mapping
  page), e.g. RG-CR "Radiografie craniu" -> "Craniu"; without a code, the anatomy the
  name parser (``rxprotocols``) finds;
* **projection**: ``classify.view_code`` (the DICOM view, else the name), else the name
  parser's (DV, DP ...);
* **size band**: the band the protocol name states (what the operator picked), else the
  patient's own weight or age in the project's group scheme (``ext-body-weight``,
  ``ext-head-age``). A patient-derived band is marked ``source = "patient"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import classify, rxprotocols
from .db.models import Classification, Code, CodeSystem
from .patient_age import band_for_age, band_for_weight
from .stats import load_bands

NOMENCLATOR = "ro-nom-rx"
WEIGHT_SCHEME, AGE_SCHEME = "ext-body-weight", "ext-head-age"
DAYS_PER_MONTH = 30.4375
_PROJ = {"LL": "LAT", "TG": "TG", "AX": "AX"}
_PREFIX = ("radiografie ", "radiografia ")


_parse = lru_cache(maxsize=4096)(rxprotocols.parse)  # a unit has a few hundred distinct names
_view = lru_cache(maxsize=4096)(classify.view_code)


@dataclass(frozen=True)
class Group:
    anatomy: str
    projection: str
    kind: str            # "weight" | "age" | "none"
    lo: float | None     # kg or months
    hi: float | None     # rxprotocols.INF for an open end
    label: str           # "5-15 kg", ">50 kg", "1-6 y", "" when no band
    source: str          # "name" | "patient" | "hidden" (non-standard name band, hidden) | ""

    @property
    def name(self) -> str:
        """"Torace AP · 5-15 kg": the group a chart shows, the same on every device."""
        return " · ".join(x for x in (" ".join(x for x in (self.anatomy, self.projection) if x), self.label) if x)


def anatomy_label(meaning: str | None) -> str:
    """"Radiografie coloană lombară" -> "Coloană lombară"."""
    m = (meaning or "").strip()
    for p in _PREFIX:
        if m.lower().startswith(p):
            m = m[len(p):]
            break
    return m[:1].upper() + m[1:]


def rx_codes(session: Session, event_ids: list[int], chunk: int = 5000) -> dict[int, tuple[str, str]]:
    """event id -> (code, meaning) of its own nomenclator exam code (when it has one)."""
    out: dict[int, tuple[str, str]] = {}
    ids = sorted(set(event_ids))
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        for eid, code, meaning in session.execute(
                select(Classification.event_id, Code.code, Code.meaning)
                .join(Code, Code.id == Classification.code_id)
                .join(CodeSystem, CodeSystem.id == Code.system_id)
                .where(CodeSystem.key == NOMENCLATOR, Classification.event_id.in_(part))):
            out.setdefault(eid, (code, meaning))
    return out


def _months(value: float, unit: str | None) -> float:
    return {"y": value * 12, "mo": value, "d": value / DAYS_PER_MONTH}.get(unit or "y", value * 12)


def _label(kind: str, lo: float | None, hi: float | None) -> str:
    inf = rxprotocols.INF
    if kind == "weight":
        unit, f = "kg", (lambda x: f"{x:g}")
    else:
        # whole years -> years ('1-6 y', '>18 y'); anything under a year -> months ('0-3 mo')
        years = all(v is None or v == inf or v % 12 == 0 for v in (lo, hi))
        unit, f = ("y", (lambda x: f"{x / 12:g}")) if years else ("mo", (lambda x: f"{x:g}"))
    if hi is None or hi == inf:
        return f">{f(lo)} {unit}"
    if lo is None or lo == 0 and kind == "weight":
        return f"<{f(hi)} {unit}"
    return f"{f(lo)}-{f(hi)} {unit}"


class Describer:
    """Build the group of exposures; loads the band schemes and the grid once."""

    def __init__(self, session: Session):
        from .grid import Grid

        self.grid = Grid(session)
        self.bands = {"weight": load_bands(session, WEIGHT_SCHEME)[1], "age": load_bands(session, AGE_SCHEME)[1]}

    def kind_for(self, code: str | None) -> str:
        """ICRP 135: head exams are grouped by age, the others by weight."""
        return self.grid.parameter(NOMENCLATOR, code) or "weight"

    def is_standard(self, kind: str, lo: float | None, hi: float | None) -> bool:
        """Is a band written in a name one of the scheme's (ICRP 135 / RP 185, extended)?"""
        if lo is None:
            return False
        for b in self.bands[kind]:
            blo = (b.lower if kind == "weight" else _months(b.lower, b.lower_unit)) if b.lower is not None else 0.0
            bhi = ((b.upper if kind == "weight" else _months(b.upper, b.upper_unit))
                   if b.upper is not None else rxprotocols.INF)
            if abs(blo - lo) < 1e-6 and (hi == bhi or (hi is not None and abs(bhi - hi) < 1e-6)):
                return True
        return False

    def own_kind(self, protocol: str | None, code: tuple[str, str] | None) -> str:
        """The size type of an exam: what its name's band is (weight or age), else the exam's
        ICRP 135 type (head by age, the rest by weight). An exam is never shown in the
        other type's view."""
        parsed = _parse(protocol)
        if parsed and parsed.kind != "none":
            return parsed.kind
        return self.kind_for(code[0] if code else None)

    def patient_band(self, kind: str, age_days, weight_kg) -> tuple[float, float, str] | None:
        if kind == "weight":
            b = band_for_weight(self.bands["weight"], weight_kg) if weight_kg and weight_kg > 0 else None
            if b is None:
                return None
            lo = b.lower if b.lower is not None else 0.0
            hi = b.upper if b.upper is not None else rxprotocols.INF
        else:
            b = band_for_age(self.bands["age"], days=int(age_days)) if age_days is not None else None
            if b is None:
                return None
            lo = _months(b.lower, b.lower_unit) if b.lower is not None else 0.0
            hi = _months(b.upper, b.upper_unit) if b.upper is not None else rxprotocols.INF
        return lo, hi, _label(kind, lo, hi)

    def describe(self, protocol: str | None, view: str | None, code: tuple[str, str] | None,
                 age_days, weight_kg, kind: str | None = None, other: str = "keep") -> Group | None:
        """``kind`` is the band the chart wants ("weight" / "age"), or None to follow the
        exam (head by age, the rest by weight). ``weight_kg`` None when it is only an estimate.
        ``other`` says what to do with a name band that is not in the scheme (the Ysio has
        "0-15", ">50", "5-20" ...): "keep" it as written, "hide" the exposure (source
        "hidden"), or "reband" it from the patient's own weight or age."""
        parsed = _parse(protocol)
        if code:
            anatomy = anatomy_label(code[1])
        elif parsed:
            anatomy = parsed.anatomy
        else:
            return None
        v = _view(view, protocol)
        proj = _PROJ.get(v, v) if v else (parsed.projection if parsed else "")
        if not proj and code and code[0] in classify.FRONTAL_BY_DEFAULT:
            proj = "AP"
        named = parsed and parsed.kind != "none" and (kind is None or parsed.kind == kind)
        if named and (other == "keep" or self.is_standard(parsed.kind, parsed.lo, parsed.hi)):
            return Group(anatomy, proj, parsed.kind, parsed.lo, parsed.hi, parsed.label, "name")
        if named and other == "hide":
            return Group(anatomy, proj, parsed.kind, parsed.lo, parsed.hi, parsed.label, "hidden")
        want = kind or (parsed.kind if named else self.kind_for(code[0] if code else None))
        got = self.patient_band(want, age_days, weight_kg)
        if got:
            return Group(anatomy, proj, want, got[0], got[1], got[2], "patient")
        return Group(anatomy, proj, "none", None, None, "", "")
