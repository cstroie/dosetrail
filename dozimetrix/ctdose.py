"""CT effective dose by the DLP method: E = DLP × k, with k (mSv per mGy·cm) by body region, age
and the CTDI phantom of the DLP.

**A population-level estimate, not a patient's dose.** The coefficient is for a reference phantom of
the patient's age; it ignores the patient's size, the exact range and the scanner's spectrum (a
small child can be off by a factor of two or more), so it is for comparing exams, rooms and years
and for the regulatory return's "estimated effective dose", never for a letter to a patient or a
risk estimate for one. Organ doses (Monte Carlo per patient size) are out of scope.

**No coefficients ship.** They come from a published set that the medical physicist chooses (ICRP
103 based: Deak 2010, Shrimpton, Brady 2012, ...) and enters with ``dozimetrix ct-kfactor set``
(region, age_to_years, phantom_cm, k, source); the source is kept with every row and shown with the
result. ``docs/examples/ct-kfactors-brady2012.csv`` is one such set, transcribed from Table 2 of
Brady et al., Br J Radiol 2012;85:1488 (their own ICRP 103 coefficients; check it against the
paper before adopting it). A study without a coefficient for its region, age and phantom has no
estimate (the reason is given), never a guess from another phantom: a 16 cm and a 32 cm DLP of
the same scan differ by a factor of two.

The region comes from the study's national exam code (``REGION``); a chest-abdomen or
chest-abdomen-pelvis study has region ``trunk``, which needs its own coefficient. Extremities and
studies with no exam code have none.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import Classification, Code, CodeSystem, CtKFactor, Event, Study

NATIONAL = "ro-drl-ms838-2025"
REGIONS = ("head", "neck", "chest", "abdomen_pelvis", "trunk")
REGION = {
    "CT-CAP": "head", "CT-SINUS": "head", "CT-GAT": "neck", "CT-COL-CERV": "neck",
    "CT-TORACE": "chest", "CT-COL-TOR": "chest",
    "CT-ABDOMEN": "abdomen_pelvis", "CT-PELVIS": "abdomen_pelvis",
    "CT-ABDOMEN-PELVIS": "abdomen_pelvis", "CT-COL-LOMB": "abdomen_pelvis",
    "CT-TORACE-ABDOMEN": "trunk", "CT-TRUNCHI": "trunk",
}
# regions in which the uterus is in or at the primary beam (conceptus.py)
UTERUS_IN_BEAM = {"abdomen_pelvis", "trunk"}
DAYS_PER_YEAR = 365.25


class CtDoseError(ValueError):
    pass


class KTable:
    def __init__(self, rows: list[tuple[str, float | None, int, float, str]]):
        self._by: dict[tuple[str, int], list[tuple[float, float, str]]] = {}
        for region, age_to, phantom, k, source in rows:
            self._by.setdefault((region, phantom), []).append(
                (float("inf") if age_to is None else age_to, k, source))
        for pts in self._by.values():
            pts.sort()
        # the adult row of a region starts above its highest paediatric limit, whatever the
        # phantom: a 32 cm adult coefficient must not be taken for a child
        self._adult_from: dict[str, float] = {}
        for (region, _p), pts in self._by.items():
            finite = [lim for lim, _k, _s in pts if lim != float("inf")]
            if finite:
                self._adult_from[region] = max(self._adult_from.get(region, 0.0), max(finite))

    def __bool__(self) -> bool:
        return bool(self._by)

    def get(self, region: str, age_days: int | None, phantom_cm: int | None
            ) -> tuple[float, str] | None:
        """(k, source) of the row with the smallest age limit at or above the age (an unknown age
        has none: the adult row would understate a child), for the region and phantom."""
        if phantom_cm is None or age_days is None:
            return None
        years = age_days / DAYS_PER_YEAR
        for limit, k, source in self._by.get((region, phantom_cm), ()):
            if years <= limit and (limit != float("inf")
                                   or years > self._adult_from.get(region, 0.0)):
                return k, source
        return None


def load(session: Session) -> KTable:
    return KTable([(c.region, c.age_to_years, c.phantom_cm, c.k, c.source)
                   for c in session.scalars(select(CtKFactor))])


def set_rows(session: Session, rows: list[tuple[str, float | None, int, float, str]]) -> int:
    """Replace the coefficients (validated); returns how many."""
    from sqlalchemy import delete

    seen = set()
    for region, age_to, phantom, k, source in rows:
        if region not in REGIONS:
            raise CtDoseError(f"region is one of {', '.join(REGIONS)}: {region!r}")
        if phantom not in (16, 32):
            raise CtDoseError("phantom_cm is 16 or 32")
        if not 0 < k < 0.5:
            raise CtDoseError(f"k {k} is not a plausible mSv per mGy·cm")
        if not source.strip():
            raise CtDoseError("each coefficient needs its source")
        if age_to is not None and age_to <= 0:
            raise CtDoseError("age_to_years is positive, or empty for adults")
        if (region, age_to, phantom) in seen:
            raise CtDoseError(f"{region} {age_to} {phantom} cm is repeated")
        seen.add((region, age_to, phantom))
    session.execute(delete(CtKFactor))
    session.add_all(CtKFactor(region=r, age_to_years=a, phantom_cm=p, k=k, source=s.strip()[:255])
                    for r, a, p, k, s in rows)
    session.flush()
    return len(rows)


def study_regions(session: Session, study_ids: list[int]) -> dict[int, str]:
    """Study id -> region, from the study's national exam code."""
    system = session.scalar(select(CodeSystem.id).where(CodeSystem.key == NATIONAL))
    out: dict[int, str] = {}
    if system is None:
        return out
    for sid, code in session.execute(
            select(Classification.study_id, Code.code)
            .join(Code, (Code.id == Classification.code_id) & (Code.system_id == system))
            .where(Classification.study_id.in_(study_ids), Classification.event_id.is_(None))):
        if code in REGION:
            out[sid] = REGION[code]
    return out


def estimate(s: Study, events: list[Event], region: str | None, table: KTable) -> dict:
    """The effective dose of one CT study: Σ DLP × k over its series (localizers left out).
    ``msv`` is None when no coefficient applies; ``complete`` is false when some series' DLP had
    none (``msv`` then covers the rest only)."""
    base = {"msv": None, "region": region, "complete": False, "sources": []}
    if not table:
        return {**base, "reason": "no conversion coefficients entered (dozimetrix ct-kfactor)"}
    if region is None:
        return {**base, "reason": "no body region (the study has no national exam code)"}
    if s.age_days is None:
        return {**base, "reason": "age unknown"}
    series = [e for e in events if not e.is_localizer and e.dlp]
    if not series:
        return {**base, "reason": "no series DLP"}
    total, covered, sources, missing = 0.0, 0.0, set(), 0.0
    for e in series:
        hit = table.get(region, s.age_days, e.ct_phantom_cm)
        if hit is None:
            missing += e.dlp
            continue
        total += e.dlp * hit[0]
        covered += e.dlp
        sources.add(hit[1])
    if not covered:
        phantoms = sorted({e.ct_phantom_cm for e in series}, key=str)
        return {**base, "reason": f"no coefficient for {region}, this age and phantom "
                                  f"{', '.join(str(p) for p in phantoms)} cm"}
    return {"msv": total, "region": region, "complete": not missing,
            "dlp_covered": covered, "dlp_missing": missing, "sources": sorted(sources),
            "reason": None if not missing else "some series have no coefficient (phantom)"}


def for_study(session: Session, study_id: int, table: KTable | None = None) -> dict | None:
    s = session.get(Study, study_id)
    if s is None or s.modality not in ("CT", "PT"):
        return None
    events = list(session.scalars(select(Event).where(Event.study_id == study_id)))
    return estimate(s, events, study_regions(session, [study_id]).get(study_id),
                    table if table is not None else load(session))
