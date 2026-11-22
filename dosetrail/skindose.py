"""Skin dose map and peak skin dose estimate for fluoroscopy and interventional studies.

This is DoseTrail's own model. It is an **estimate**, not a measurement, and it is
labelled as such wherever it is shown. Every assumption is returned with the result.

Patient
* An elliptical cylinder around the body axis; the patient lies supine and head first
  unless the RDSR says prone.
* Region: the head (skull length and width by age) for neuro, cerebral and carotid
  procedures (description or national code), otherwise the trunk (depth and width of
  reference children and adults, scaled by weight when it is known).
* The surface is unfolded as angle around the body (0° anterior, 90° the patient's left,
  180° posterior, 270° right) × cranio-caudal position (cm, + toward the head) relative to
  the isocentre.

Beam (per irradiation event)
* Direction from the C-arm angles: primary (LAO +), secondary (cranial +). No angles means
  a posterior-anterior beam (tube under the table).
* The isocentre sits on the body axis: the table position cannot be related to the
  patient without a registration, so the map shows the distribution around the body and
  along it near the isocentre.
* Source–isocentre distance from the RDSR (113748); otherwise 750 mm (0.65 × SID for
  short SIDs).
* Reference point at the RDSR distance (113737); otherwise 150 mm before the isocentre
  (IEC 60601-2-43).
* Field size at the reference point = DAP / Ka,r (cm²), with the aspect from the
  collimated field when reported; otherwise the collimated field scaled to the reference
  point, otherwise 15 × 15 cm.

Dose at a skin point inside the field, where the beam enters
* Ka,r × (d_RP / d_point)² × backscatter 1.3 × tissue/air 1.06 × table and mattress 0.8
  when the beam comes from under the table.

Events with the same geometry are pooled, so a long procedure costs little.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

BSF, F_TISSUE, T_TABLE = 1.3, 1.06, 0.8
DEFAULT_DISO_MM, RP_BEFORE_ISO_MM, DEFAULT_FIELD_CM = 750.0, 150.0, 15.0
THETA_STEP, Z_STEP, Z_HALF = 3, 1.0, 30.0  # degrees, cm, cm
# trunk semi-axes (cm): (age years, AP half-depth, lateral half-width), reference weight kg
_TRUNK = [(0, 5.0, 6.0, 3.3), (1, 6.0, 7.5, 9.6), (5, 7.5, 9.5, 18.0), (10, 9.0, 12.0, 31.0),
          (15, 10.5, 14.5, 56.0), (18, 11.5, 17.0, 70.0)]
# head (transverse ellipse): (age years, half length front-back, half width), reference kg
_HEAD = [(0, 6.0, 4.75, 3.3), (1, 7.5, 6.0, 9.6), (5, 8.5, 6.75, 18.0), (10, 9.0, 7.0, 31.0),
         (15, 9.5, 7.5, 56.0), (18, 9.5, 7.5, 70.0)]
HEAD_PATTERN = re.compile(r"neuro|cerebr|crani|\bhead\b|carotid|\bcap\b|willis|intracran")
HEAD_CODES = {"A1", "A2", "EC", "AG-CR", "AG-CERV", "AG-CRCC"}


@dataclass(frozen=True)
class Phantom:
    a: float  # lateral semi-axis, cm
    b: float  # antero-posterior semi-axis, cm
    note: str


def phantom(age_days: int | None, weight_kg: float | None, region: str = "trunk") -> Phantom:
    age = (age_days or 0) / 365.25 if age_days is not None else 18.0
    pts = _HEAD if region == "head" else _TRUNK
    if age >= pts[-1][0]:
        _, b, a, w0 = pts[-1]
    else:
        for (x0, b0, a0, w0_), (x1, b1, a1, w1) in zip(pts, pts[1:], strict=False):
            if x0 <= age < x1:
                f = (age - x0) / (x1 - x0)
                b, a, w0 = b0 + f * (b1 - b0), a0 + f * (a1 - a0), w0_ + f * (w1 - w0_)
                break
    note = f"{region}: elliptical cylinder {2 * a:.0f} × {2 * b:.0f} cm (width × depth) " \
        "for age " + (f"{age:.1f} y" if age_days is not None else "unknown (adult assumed)")
    if weight_kg and weight_kg > 0 and region == "trunk":
        k = min(max(math.sqrt(weight_kg / w0), 0.7), 1.5)
        a, b = a * k, b * k
        note += f", scaled ×{k:.2f} for {weight_kg:g} kg"
    return Phantom(a, b, note)


def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v) if n else v


def _dot(u, v):
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def _cross(u, v):
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


@dataclass
class Beam:
    alpha: float
    beta: float
    d_iso: float  # cm
    d_rp: float  # cm
    w_rp: float  # cm
    h_rp: float  # cm
    prone: bool
    kar_mgy: float = 0.0
    n: int = 0


def beam_for(e, notes: set[str]) -> Beam | None:
    """Geometry of one event (an Event row or anything with the same attributes)."""
    kar = e.rp_dose
    if not kar or kar <= 0:
        return None
    sid = e.sid_mm
    if e.dist_source_iso_mm:
        d_iso = e.dist_source_iso_mm
    else:
        d_iso = min(DEFAULT_DISO_MM, 0.65 * sid) if sid else DEFAULT_DISO_MM
        notes.add("source–isocentre distance assumed (750 mm, or 0.65 × SID)")
    if e.dist_source_rp_mm:
        d_rp = e.dist_source_rp_mm
    else:
        d_rp = d_iso - RP_BEFORE_ISO_MM
        notes.add("reference point assumed 15 cm before the isocentre (IEC)")
    area = (e.dap / kar) if e.dap and e.dap > 0 else None  # Gy.cm2 / Gy = cm2 at the RP
    aspect = (e.field_w_mm / e.field_h_mm) if e.field_w_mm and e.field_h_mm else 1.0
    if area and 1.0 <= area <= 3000.0:
        h = math.sqrt(area / aspect)
        w, hh = h * aspect, h
    elif e.field_w_mm and e.field_h_mm and sid:
        w, hh = e.field_w_mm / 10 * d_rp / sid, e.field_h_mm / 10 * d_rp / sid
        notes.add("field from the collimated field size (no DAP/Ka,r ratio)")
    else:
        w = hh = DEFAULT_FIELD_CM
        notes.add("field assumed 15 × 15 cm at the reference point")
    if e.primary_angle is None and e.secondary_angle is None:
        notes.add("no C-arm angles: posterior–anterior beam assumed")
    pos = (getattr(e, "patient_position", None) or "").lower()
    return Beam(alpha=e.primary_angle or 0.0, beta=e.secondary_angle or 0.0,
                d_iso=d_iso / 10, d_rp=d_rp / 10, w_rp=w, h_rp=hh, prone="prone" in pos,
                kar_mgy=kar * 1000, n=1)


def pool(beams: list[Beam]) -> list[Beam]:
    """Sum Ka,r of events with the same geometry (1°, 1 cm)."""
    acc: dict[tuple, Beam] = {}
    for b in beams:
        key = (round(b.alpha), round(b.beta), round(b.d_iso), round(b.d_rp), round(b.w_rp),
               round(b.h_rp), b.prone)
        if key in acc:
            acc[key].kar_mgy += b.kar_mgy
            acc[key].n += b.n
        else:
            acc[key] = Beam(*[getattr(b, f) for f in ("alpha", "beta", "d_iso", "d_rp", "w_rp",
                                                       "h_rp", "prone", "kar_mgy", "n")])
    return list(acc.values())


def compute(events, age_days: int | None = None, weight_kg: float | None = None,
            region: str = "trunk") -> dict:
    notes: set[str] = set()
    ph = phantom(age_days, weight_kg, region)
    beams, skipped = [], 0
    for e in events:
        b = beam_for(e, notes)
        if b is None:
            skipped += 1
        else:
            beams.append(b)
    thetas = list(range(0, 360, THETA_STEP))
    nz = int(2 * Z_HALF / Z_STEP) + 1
    zs = [round(-Z_HALF + i * Z_STEP, 2) for i in range(nz)]
    grid = [[0.0] * len(thetas) for _ in zs]
    # surface points and outward normals (patient coordinates: x left, y anterior, z head)
    surf = []
    for ti, th in enumerate(thetas):
        r = math.radians(th)
        x, y = ph.a * math.sin(r), ph.b * math.cos(r)
        n = _norm((math.sin(r) / ph.a, math.cos(r) / ph.b, 0.0))
        surf.append((ti, x, y, n))
    for b in pool(beams):
        al, be = math.radians(b.alpha), math.radians(b.beta)
        d = _norm((math.sin(al) * math.cos(be), math.cos(al) * math.cos(be), math.sin(be)))
        if b.prone:
            d = (-d[0], -d[1], d[2])
        src = (-b.d_iso * d[0], -b.d_iso * d[1], -b.d_iso * d[2])
        e1 = _cross(d, (0.0, 0.0, 1.0))
        e1 = _norm(e1) if abs(_dot(d, (0, 0, 1))) < 0.999 else (1.0, 0.0, 0.0)
        e2 = _cross(e1, d)
        table = T_TABLE if src[1] < -ph.b else 1.0  # tube under the table: through it
        k = b.kar_mgy * BSF * F_TISSUE * table * b.d_rp ** 2
        hw0, hh0 = b.w_rp / 2 / b.d_rp, b.h_rp / 2 / b.d_rp
        for ti, x, y, n in surf:
            vx, vy = x - src[0], y - src[1]
            if vx * n[0] + vy * n[1] >= 0:  # (z component of n is 0) not an entrance point
                continue
            for zi, z in enumerate(zs):
                vz = z - src[2]
                t = vx * d[0] + vy * d[1] + vz * d[2]
                if t <= 0:
                    continue
                if abs(vx * e1[0] + vy * e1[1] + vz * e1[2]) > hw0 * t:
                    continue
                if abs(vx * e2[0] + vy * e2[1] + vz * e2[2]) > hh0 * t:
                    continue
                grid[zi][ti] += k / (vx * vx + vy * vy + vz * vz)
    peak, at = 0.0, None
    for zi, row in enumerate(grid):
        for ti, v in enumerate(row):
            if v > peak:
                peak, at = v, {"theta": thetas[ti], "z_cm": zs[zi]}
    # cell area: arc length on the ellipse × Z step
    arc = [math.hypot(ph.a * math.cos(math.radians(t)), ph.b * math.sin(math.radians(t)))
           * math.radians(THETA_STEP) for t in thetas]

    def area_over(mgy):
        return round(sum(arc[ti] * Z_STEP for row in grid for ti, v in enumerate(row)
                         if v >= mgy), 1)

    total = sum(b.kar_mgy for b in beams)
    return {
        "estimate": True,
        "psd_mgy": round(peak, 1) if beams else None, "psd_at": at,
        "kar_total_mgy": round(total, 1), "psd_over_kar": round(peak / total, 2) if total else None,
        "area_cm2_over_2gy": area_over(2000), "area_cm2_over_3gy": area_over(3000),
        "theta": thetas, "z_cm": zs,
        "dose_mgy": [[round(v, 2) for v in row] for row in grid],
        "events_used": len(beams), "events_skipped": skipped,
        "geometries": len(pool(beams)) if beams else 0,
        "region": region,
        "assumptions": {
            "phantom": ph.note,
            "factors": f"backscatter {BSF}, tissue/air {F_TISSUE}, table and mattress "
                       f"{T_TABLE} for beams from under the table",
            "isocentre": "on the body axis (table position not registered to the patient)",
            "notes": sorted(notes),
        },
    }


SKIN_MODALITIES = ("RF", "XA")


def region_of(session, study) -> str:
    """'head' for neuro / cerebral / carotid procedures (text or national code), else trunk."""
    from sqlalchemy import select

    from .db.models import Classification, Code

    text = " ".join(x for x in (study.study_description, study.procedure_name,
                                study.requested_procedure) if x).lower()
    if HEAD_PATTERN.search(text):
        return "head"
    codes = set(session.scalars(select(Code.code).join(Classification,
                                                       Classification.code_id == Code.id)
                                .where(Classification.study_id == study.id)))
    return "head" if codes & HEAD_CODES else "trunk"


def for_study(session, study_id: int) -> dict | None:
    from sqlalchemy import select

    from .db.models import Event, Study

    s = session.get(Study, study_id)
    if s is None:
        return None
    events = list(session.scalars(select(Event).where(Event.study_id == study_id)))
    return {**compute(events, s.age_days, s.weight_kg, region_of(session, s)),
            "modality": s.modality}


def update_psd(session, study_ids=None) -> dict:
    """Store the estimated peak skin dose on fluoroscopy / interventional studies."""
    from collections import defaultdict

    from sqlalchemy import select

    from .db.models import Event, Study

    q = select(Study).where(Study.modality.in_(SKIN_MODALITIES))
    if study_ids is not None:
        q = q.where(Study.id.in_(list(study_ids)))
    stats = {"studies": 0, "with_psd": 0}
    studies = list(session.scalars(q))
    for i in range(0, len(studies), 500):
        chunk = studies[i:i + 500]
        events = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_([s.id for s in chunk]))):
            events[e.study_id].append(e)
        for s in chunk:
            r = compute(events.get(s.id, []), s.age_days, s.weight_kg, region_of(session, s))
            s.psd_mgy = r["psd_mgy"]
            stats["studies"] += 1
            stats["with_psd"] += s.psd_mgy is not None
        session.flush()
    return stats
