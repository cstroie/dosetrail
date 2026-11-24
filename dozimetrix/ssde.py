"""Size-specific dose estimate (SSDE) for CT, calculated from the water-equivalent diameter.

SSDE = f(Dw) × CTDIvol, with the published exponential fits f = a·exp(−b·Dw), Dw in cm:

* body, CTDIvol on the 32 cm phantom: a = 3.704369, b = 0.03671937 (AAPM Report 204, eq.
  in its appendix; Report 220 Table A-1 uses the same fit with Dw in place of the effective
  diameter); tabulated for 8–45 cm;
* body, 16 cm phantom: a = 1.874799, b = 0.03871313 (AAPM 204); tabulated for 6–45 cm;
* head, CTDIvol on the 16 cm phantom: a = 1.9852, b = 0.0486 (AAPM Report 293, Eq. 1).

Dw comes only from a measurement: the dose report's Water Equivalent Diameter (113980),
as the scanner computed it from the images. AAPM 204 gives no age-based diameter table, and
no other fallback is used. Without Dw, or outside the tabulated range, or for a head series
on the 32 cm phantom, nothing is calculated. A scanner-reported SSDE (113930) stays in
``event.ssde``; the calculated value goes to ``event.ssde_calc`` with ``ssde_method``.
"""

from __future__ import annotations

import math

BODY = {32: (3.704369, 0.03671937, 8.0, 45.0), 16: (1.874799, 0.03871313, 6.0, 45.0)}
HEAD16 = (1.9852, 0.0486)


def factor(dw_cm: float, phantom_cm: int, head: bool) -> tuple[float, str] | None:
    """(f, method) or None when no published factor applies."""
    if dw_cm is None or dw_cm <= 0:
        return None
    if head:
        if phantom_cm != 16:
            return None
        a, b = HEAD16
        return a * math.exp(-b * dw_cm), "aapm293_head16"
    if phantom_cm not in BODY:
        return None
    a, b, lo, hi = BODY[phantom_cm]
    if not lo <= dw_cm <= hi:
        return None
    return a * math.exp(-b * dw_cm), f"aapm204_body{phantom_cm}"


def for_event(ev, head: bool) -> tuple[float, str] | None:
    """(SSDE mGy, method) for a CT event with CTDIvol, phantom and a measured Dw. ``head``:
    a head series (quality._head_series: the protocol's own exam, else the study's)."""
    if ev.is_localizer or ev.ctdivol is None or not ev.wed_mm or not ev.ct_phantom_cm:
        return None
    f = factor(ev.wed_mm / 10, ev.ct_phantom_cm, head)
    if f is None:
        return None
    return round(f[0] * ev.ctdivol, 3), f[1]


def update(session, study_ids=None) -> int:
    """Recalculate ssde_calc for CT events (all, or of the given studies). Returns changes."""
    from sqlalchemy import select

    from .db.models import Event, Study
    from .quality import _head_series, _national_codes

    q = (select(Event).join(Study, Study.id == Event.study_id)
         .where(Study.modality.in_(("CT", "PT", "NM")), Event.wed_mm.is_not(None)))
    if study_ids is not None:
        q = q.where(Event.study_id.in_(list(study_ids)))
    events = list(session.scalars(q))
    codes = _national_codes(session, sorted({e.study_id for e in events}))
    n = 0
    for ev in events:
        head = _head_series(ev, codes.get(ev.study_id, {}).get(None))
        val, method = for_event(ev, head) or (None, None)
        if (ev.ssde_calc, ev.ssde_method) != (val, method):
            ev.ssde_calc, ev.ssde_method = val, method
            n += 1
    session.flush()
    return n
