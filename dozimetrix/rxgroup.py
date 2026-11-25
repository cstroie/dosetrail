"""One vocabulary for radiography exposures across devices: anatomy, projection, size band (stored per exposure in `exposure_band`, see `update`).

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

import datetime as dt
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from . import classify, rxprotocols
from .db.models import Classification, Code, CodeSystem
from .patient_age import band_for_age, band_for_weight
from .stats import load_bands

NOMENCLATOR = "ro-nom-rx"
WEIGHT_SCHEME, AGE_SCHEME = "ext-body-weight", "ext-head-age"  # European: ICRP 135 / RP 185, extended
NATIONAL_AGE_SCHEME = "ro-drl-age"  # national: OMS 838/2025
DAYS_PER_MONTH = 30.4375
_PROJ = {"LL": "LAT", "TG": "TG", "AX": "AX"}
_PREFIX = ("radiografie ", "radiografia ")


_parse = rxprotocols.parse  # cached there, as are classify.view_code and anatomy_label
_view = classify.view_code


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


@lru_cache(maxsize=1024)
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


@dataclass(frozen=True)
class Bands:
    """What is stored per exposure (``exposure_band``): the exam code and projection, the band the
    protocol name states, and the patient's own bands in the European and national schemes."""

    code: str | None            # nomenclator exam code of the exposure (RG-CR ...)
    meaning: str | None
    projection: str
    name_kind: str | None       # "weight" (kg) | "age" (months) | None: the name states no band
    name_lo: float | None
    name_hi: float | None       # None = open end
    name_label: str | None
    age_eu: str | None          # ext-head-age band key (RP 185 / ICRP 135, extended)
    weight_eu: str | None       # ext-body-weight band key
    age_nat: str | None         # ro-drl-age band key (OMS 838/2025)


class Describer:
    """Compute, store and resolve the bands of exposures; loads the schemes and the grid once."""

    def __init__(self, session: Session):
        from .grid import Grid

        self.grid = Grid(session)
        self.bands = {"weight": load_bands(session, WEIGHT_SCHEME)[1], "age": load_bands(session, AGE_SCHEME)[1]}
        self.national = load_bands(session, NATIONAL_AGE_SCHEME)[1]
        self.info: dict[str, dict[str, tuple[float, float, str]]] = {"weight": {}, "age": {}}
        for kind, bands in self.bands.items():
            for b in bands:
                if kind == "weight":
                    lo, hi = (b.lower if b.lower is not None else 0.0), (b.upper if b.upper is not None else rxprotocols.INF)
                else:
                    lo = _months(b.lower, b.lower_unit) if b.lower is not None else 0.0
                    hi = _months(b.upper, b.upper_unit) if b.upper is not None else rxprotocols.INF
                self.info[kind][b.key] = (lo, hi, _label(kind, lo, hi))

    def kind_for(self, code: str | None) -> str:
        """ICRP 135: head exams are grouped by age, the others by weight."""
        return self.grid.parameter(NOMENCLATOR, code) or "weight"

    def is_standard(self, kind: str, lo: float | None, hi: float | None) -> bool:
        """Is a band written in a name one of the scheme's (ICRP 135 / RP 185, extended)?"""
        if lo is None:
            return False
        for blo, bhi, _ in self.info[kind].values():
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

    def kind_of(self, b: Bands) -> str:
        return b.name_kind or self.kind_for(b.code)

    # ------------------------------------------------------------------ compute (stored)

    def compute(self, protocol: str | None, view: str | None, code: tuple[str, str] | None,
                age_days, weight_kg) -> Bands:
        """The bands of one exposure. ``weight_kg`` None when it is only an estimate."""
        parsed = _parse(protocol)
        v = _view(view, protocol)
        proj = _PROJ.get(v, v) if v else (parsed.projection if parsed else "")
        if not proj and code and code[0] in classify.FRONTAL_BY_DEFAULT:
            proj = "AP"
        named = parsed is not None and parsed.kind != "none"
        age_eu = weight_eu = age_nat = None
        if age_days is not None:
            b = band_for_age(self.bands["age"], days=int(age_days))
            age_eu = b.key if b else None
            n = band_for_age(self.national, days=int(age_days))
            age_nat = n.key if n else None
        if weight_kg and weight_kg > 0:
            b = band_for_weight(self.bands["weight"], weight_kg)
            weight_eu = b.key if b else None
        return Bands(code[0] if code else None, code[1] if code else None, proj or "",
                     parsed.kind if named else None, parsed.lo if named else None,
                     None if (not named or parsed.hi == rxprotocols.INF) else parsed.hi,
                     parsed.label if named else None, age_eu, weight_eu, age_nat)

    # ------------------------------------------------------------------ resolve (policy)

    def patient_band(self, kind: str, b: Bands) -> tuple[float, float, str] | None:
        key = b.weight_eu if kind == "weight" else b.age_eu
        return self.info[kind].get(key) if key else None

    def resolve(self, protocol: str | None, b: Bands, kind: str | None = None, other: str = "keep") -> Group | None:
        """``kind`` is the band the chart wants ("weight" / "age"), or None to follow the exam
        (head by age, the rest by weight). ``other`` says what to do with a name band that is not
        in the scheme (the Ysio has "0-15", ">50", "5-20" ...): "keep" it as written, "hide" the
        exposure (source "hidden"), or "reband" it from the patient's own weight or age."""
        parsed = _parse(protocol)
        if b.meaning:
            anatomy = anatomy_label(b.meaning)
        elif parsed:
            anatomy = parsed.anatomy
        else:
            return None
        hi = rxprotocols.INF if b.name_kind and b.name_hi is None else b.name_hi
        named = b.name_kind is not None and (kind is None or b.name_kind == kind)
        if named and (other == "keep" or self.is_standard(b.name_kind, b.name_lo, hi)):
            return Group(anatomy, b.projection, b.name_kind, b.name_lo, hi, b.name_label or "", "name")
        if named and other == "hide":
            return Group(anatomy, b.projection, b.name_kind, b.name_lo, hi, b.name_label or "", "hidden")
        want = kind or (b.name_kind if named else self.kind_for(b.code))
        got = self.patient_band(want, b)
        if got:
            return Group(anatomy, b.projection, want, got[0], got[1], got[2], "patient")
        return Group(anatomy, b.projection, "none", None, None, "", "")

    def describe(self, protocol: str | None, view: str | None, code: tuple[str, str] | None,
                 age_days, weight_kg, kind: str | None = None, other: str = "keep") -> Group | None:
        """compute + resolve in one step (what is not stored yet)."""
        return self.resolve(protocol, self.compute(protocol, view, code, age_days, weight_kg), kind, other)


# ---------------------------------------------------------------------- the stored table


def _codes_full(session: Session, event_ids: list[int], chunk: int = 5000) -> dict[int, tuple[int, str, str]]:
    out: dict[int, tuple[int, str, str]] = {}
    ids = sorted(set(event_ids))
    for i in range(0, len(ids), chunk):
        for eid, cid, code, meaning in session.execute(
                select(Classification.event_id, Code.id, Code.code, Code.meaning)
                .join(Code, Code.id == Classification.code_id)
                .join(CodeSystem, CodeSystem.id == Code.system_id)
                .where(CodeSystem.key == NOMENCLATOR, Classification.event_id.in_(ids[i:i + chunk]))):
            out.setdefault(eid, (cid, code, meaning))
    return out


def update(session: Session, study_ids: Iterable[int] | None = None, describer: Describer | None = None,
           chunk: int = 2000, progress=None) -> Counter:
    """(Re)compute ``exposure_band`` for radiography studies: the given ones, or all. Run after
    the exposures are classified (their exam code is stored with the bands) and whenever age or
    weight change (derive() does both). Replaces the rows of the studies it touches."""
    from .db.models import Event, ExposureBand, SizeSource, Study

    from .stats import StatsError

    stats: Counter = Counter()
    try:
        d = describer or Describer(session)
    except StatsError:  # group schemes not seeded yet: nothing to band (imports must not fail on it)
        stats["no_schemes"] += 1
        return stats
    if study_ids is None:
        ids = list(session.scalars(select(Study.id).where(Study.modality.in_(("DX", "CR"))).order_by(Study.id)))
    else:
        ids = sorted(set(study_ids))
    now = dt.datetime.now()
    for i in range(0, len(ids), chunk):
        part = ids[i:i + chunk]
        studies = {r[0]: r for r in session.execute(
            select(Study.id, Study.age_days, Study.weight_kg, Study.weight_source)
            .where(Study.id.in_(part), Study.modality.in_(("DX", "CR"))))}
        session.execute(delete(ExposureBand).where(ExposureBand.study_id.in_(part)))
        if not studies:
            continue
        events = session.execute(select(Event.id, Event.study_id, Event.protocol, Event.view)
                                 .where(Event.study_id.in_(list(studies)))).all()
        codes = _codes_full(session, [e[0] for e in events])
        rows = []
        for eid, sid, proto, view in events:
            _, age, w, wsrc = studies[sid]
            if wsrc == SizeSource.estimated_reference:
                w = None  # a weight taken from the growth chart says nothing about this patient
            cid, code, meaning = codes.get(eid, (None, None, None))
            b = d.compute(proto, view, (code, meaning) if code else None, age, w)
            rows.append({"event_id": eid, "study_id": sid, "exam_code_id": cid, "projection": (b.projection or None) and b.projection[:8],
                         "name_kind": b.name_kind, "name_lo": b.name_lo, "name_hi": b.name_hi,
                         "name_label": b.name_label, "age_eu": b.age_eu, "weight_eu": b.weight_eu,
                         "age_nat": b.age_nat, "computed_at": now})
        if rows:
            session.execute(insert(ExposureBand), rows)
        stats["studies"] += len(studies)
        stats["exposures"] += len(rows)
        if progress and (i // chunk) % 25 == 24:
            progress(f"{stats['studies']} studies, {stats['exposures']} exposures")
    session.flush()
    return stats


def load(session: Session, event_ids: list[int], chunk: int = 5000) -> dict[int, Bands]:
    """The stored bands of the given exposures (those not filled yet are missing)."""
    from .db.models import ExposureBand

    out: dict[int, Bands] = {}
    ids = sorted(set(event_ids))
    eb = ExposureBand  # columns, not entities: 45k rows of a chart would be 45k ORM objects
    for i in range(0, len(ids), chunk):
        for r in session.execute(
                select(eb.event_id, Code.code, Code.meaning, eb.projection, eb.name_kind,
                       eb.name_lo, eb.name_hi, eb.name_label, eb.age_eu, eb.weight_eu, eb.age_nat)
                .outerjoin(Code, Code.id == eb.exam_code_id)
                .where(eb.event_id.in_(ids[i:i + chunk]))):
            out[r[0]] = Bands(r[1], r[2], r[3] or "", *r[4:])
    return out


def fetch(session: Session, d: Describer, items: list[tuple]) -> dict[int, Bands]:
    """Bands of exposures for a chart: the stored ones; an exposure not stored yet (derived
    after the last fill) is computed on the spot. ``items``: (event id, protocol, view,
    age_days, weight_kg, weight_source)."""
    from .db.models import SizeSource

    out = load(session, [i[0] for i in items])
    missing = [i for i in items if i[0] not in out]
    if missing:
        codes = _codes_full(session, [i[0] for i in missing])
        for eid, proto, view, age, w, wsrc in missing:
            _, code, meaning = codes.get(eid, (None, None, None))
            out[eid] = d.compute(proto, view, (code, meaning) if code else None, age,
                                 None if wsrc == SizeSource.estimated_reference else w)
    return out
