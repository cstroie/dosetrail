"""Conceptus (embryo / fetus) dose screening for a pregnant patient, CT and fluoroscopy.

A screening estimate to decide whether a physicist's calculation is needed, not that calculation.

**CT.** The quantity is the fetal dose normalised to the series' CTDIvol, which for an abdomen or
pelvis scan with the uterus in the primary beam was 0.56-1.30 (mean 0.94 ± 0.25) in 44
patient-specific models of 8-35 weeks' gestation (Medical Physics 2023, "Patient-specific fetal
radiation dosimetry for pregnant patients undergoing abdominal and pelvic CT imaging"; as
summarised by the search result: verify it against the paper before relying on the factors).
It is normalised to CTDIvol, not SSDE, so the estimate is the series' CTDIvol times the factor,
summed over the series (each phase is another pass), as a low / central / high range. It is
valid for the uterus in the beam; it is not a measurement of this patient and does not follow
her size or the gestational age.

* **Uterus in the beam** (abdomen, pelvis, lumbar spine, chest-abdomen(-pelvis)): the range above.
  A chest-abdomen scan may stop short of the uterus; the high end is then an overestimate.
* **Outside the beam** (head, neck, chest): only scatter reaches the conceptus, orders of
  magnitude below the thresholds; no number is given.
* **Fluoroscopy and interventional (RF, XA)**: no number. The dose depends on the field position
  and beam angles, which the stored totals do not carry: the physicist estimates it from the
  skin dose map and the geometry, and records the estimate in an incident (``incidents.py``) so the
  documented decision exists.

**Levels** (ICRP 84): below 1 mGy negligible; 1 to 50 mGy no deterministic effects, to be
documented and the risk discussed; from 50 mGy (the high end) a case-by-case dose estimate by the
physicist before any decision. The level uses the high end of the range.
"""

from __future__ import annotations

from .ctdose import UTERUS_IN_BEAM
from .db.models import Event, Study

FACTOR = (0.56, 0.94, 1.30)  # fetal dose / CTDIvol: low, central, high
NEGLIGIBLE_MGY, REVIEW_MGY = 1.0, 50.0


def level(high_mgy: float) -> str:
    if high_mgy < NEGLIGIBLE_MGY:
        return "negligible"
    return "document" if high_mgy < REVIEW_MGY else "physicist"


def estimate(s: Study, events: list[Event], region: str | None) -> dict:
    """``{"mgy": (low, central, high) | None, "level": ..., "text": short sentence}`` for a
    pregnant patient's study; ``region`` is the body region of its national exam code
    (``ctdose.REGION``)."""
    if s.modality in ("RF", "XA"):
        return {"mgy": None, "level": "physicist", "region": None,
                "text": "conceptus dose: no screening estimate for fluoroscopy; the physicist "
                        "estimates it from the field position and records it in an incident"}
    if s.modality not in ("CT", "PT"):
        return {"mgy": None, "level": None, "region": None, "text": ""}
    if region is None:
        return {"mgy": None, "level": "physicist", "region": None,
                "text": "conceptus dose: no estimate (body region unknown): check whether the "
                        "uterus was in the beam"}
    if region not in UTERUS_IN_BEAM:
        return {"mgy": None, "level": "negligible", "region": region,
                "text": f"conceptus dose: {region} scan, uterus outside the beam, scatter only"}
    ctdi = sum(e.ctdivol for e in events if not e.is_localizer and e.ctdivol)
    if not ctdi:
        return {"mgy": None, "level": "physicist", "region": region,
                "text": "conceptus dose: no estimate (no series CTDIvol)"}
    low, mid, high = (ctdi * f for f in FACTOR)
    lvl = level(high)
    return {"mgy": (low, mid, high), "level": lvl, "region": region, "ctdivol_sum": ctdi,
            "text": f"conceptus dose ≈ {mid:.3g} mGy ({low:.2g}–{high:.2g}; screening, ΣCTDIvol "
                    f"{ctdi:.3g} mGy × {FACTOR[0]}–{FACTOR[2]})"
                    + ("; individual estimate by the physicist" if lvl == "physicist" else "")}
