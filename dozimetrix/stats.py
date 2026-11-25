"""Dose statistics: one metric, grouped by any dimensions, over the studies a query selects.

* **Selection** reuses the typed query language (``api.query``), so a chart, a list and an
  export filtered by the same string cover the same studies. Exams are filtered separately
  (``exam=`` codes of the chosen system).
* **Levels:**
  * study metrics (DLP total, DAP total…) count each study once per exam;
  * exposure metrics (CTDIvol per series, DAP per exposure, kVp…) count events. CT
    localisers are excluded. Radiography exposures use their own exam code, which carries
    the projection.
* **Statistics:** n, mean, SD, median, P25, P75, min, max, using linear interpolation
  between order statistics (Hyndman–Fan type 7, as numpy, R and spreadsheets do). They are
  computed in Python, so SQLite and MariaDB give identical results.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, aliased

from .db.models import (
    Classification,
    Code,
    CodeSystem,
    Device,
    Event,
    GroupScheme,
    Patient,
    SizeSource,
    Study,
    StudyIndication,
)
from .patient_age import Band, band_for_age, band_for_weight


@dataclass(frozen=True)
class Metric:
    level: str  # study | event
    column: object
    unit: str
    label: str


METRICS: dict[str, Metric] = {
    "dlp_total": Metric("study", Study.total_dlp, "mGy.cm", "DLP total"),
    "ctdivol_max": Metric("study", Study.ctdivol_max, "mGy", "CTDIvol max"),
    "dap_total": Metric("study", Study.total_dap, "Gy.cm2", "DAP total"),
    "dap_fluoro": Metric("study", Study.total_fluoro_dap, "Gy.cm2", "DAP fluoroscopy"),
    "dap_acq": Metric("study", Study.total_acq_dap, "Gy.cm2", "DAP acquisition"),
    "kar_total": Metric("study", Study.total_rp_dose, "Gy", "Ka,r total"),
    "fluoro_time": Metric("study", Study.total_fluoro_time, "s", "Fluoroscopy time"),
    "psd": Metric("study", Study.psd_mgy, "mGy", "Peak skin dose (estimate)"),
    "exposures": Metric("study", Study.n_events, "1", "Irradiation events"),
    "dlp": Metric("event", Event.dlp, "mGy.cm", "DLP per series"),
    "ssde": Metric("event", func.coalesce(Event.ssde, Event.ssde_calc), "mGy",
                   "SSDE per series (reported, else calculated)"),
    "ctdivol": Metric("event", Event.ctdivol, "mGy", "CTDIvol per series"),
    "scan_length": Metric("event", Event.scan_length_mm, "mm", "Scan length"),
    "dap": Metric("event", Event.dap, "Gy.cm2", "DAP per exposure"),
    "kar": Metric("event", Event.rp_dose, "Gy", "Ka,r per event"),
    "kvp": Metric("event", Event.kvp, "kV", "kVp"),
    "mas": Metric("event", Event.mas, "mA.s", "mAs"),
    "ei": Metric("event", Event.exposure_index, "1", "Exposure index"),
    "di": Metric("event", Event.deviation_index, "1", "Deviation index"),
    "sid": Metric("event", Event.sid_mm, "mm", "Source–image distance"),
    "agd": Metric("event", Event.agd, "mGy", "AGD per view"),
    "agd_breast": Metric("study", func.coalesce(Study.agd_left, Study.agd_right), "mGy",
                         "AGD per breast (left, else right)"),
    "thickness": Metric("event", Event.compression_thickness_mm, "mm",
                        "Compressed breast thickness"),
    "force": Metric("event", Event.compression_force_n, "N", "Compression force"),
    "activity": Metric("study", Study.admin_activity_mbq, "MBq", "Administered activity"),
    "activity_kg": Metric("study", Study.admin_activity_mbq / Study.weight_kg, "MBq/kg",
                          "Administered activity per kg"),
}
DIMENSIONS = ("exam", "device", "age_band", "weight_band", "ref_group", "month", "quarter",
              "year", "modality", "sex", "protocol", "view", "indication")
EXPOSURE_EXAM = ("DX", "CR", "MG", "PX")  # modalities classified per exposure


def own_codes(event_id_col):
    """The event has exam codes of its own (radiography, or a CT series of another region
    than its study): then the study's codes do not apply to it."""
    from sqlalchemy import exists

    c2 = aliased(Classification)
    return exists(select(c2.id).where(c2.event_id == event_id_col))
MAX_GROUPS = 500


class StatsError(ValueError):
    pass


# ------------------------------------------------------------------ statistics


def quantile(sorted_values: list[float], q: float) -> float | None:
    """Linear interpolation between order statistics (type 7)."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    h = (n - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, n - 1)
    return sorted_values[lo] + (h - lo) * (sorted_values[hi] - sorted_values[lo])


def describe(values: Iterable[float]) -> dict:
    v = sorted(x for x in values if x is not None)
    n = len(v)
    if n == 0:
        return {"n": 0}
    mean = sum(v) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in v) / (n - 1)) if n > 1 else None
    return {"n": n, "mean": mean, "sd": sd, "min": v[0], "p25": quantile(v, 0.25),
            "median": quantile(v, 0.5), "p75": quantile(v, 0.75), "max": v[-1]}


def histogram(values: list[float], edges: list[float]) -> list[int]:
    counts = [0] * (len(edges) - 1)
    if not counts:
        return counts
    lo, hi = edges[0], edges[-1]
    width = (hi - lo) / len(counts) if hi > lo else 1
    for x in values:  # values beyond the edges go to the end bins
        i = min(max(int((x - lo) / width), 0), len(counts) - 1)
        counts[i] += 1
    return counts


# ------------------------------------------------------------------ bands


BANDS_TTL_S = 30.0  # a scheme edit reaches a long-lived session within this


def load_bands(session: Session, scheme_key: str) -> tuple[str, list[Band]]:
    """A scheme's parameter and bands. Several parts of one request build the same lookups (the
    grid, the radiography bands, the national DRLs), so a session keeps what it loaded for a
    short while: two queries per scheme saved each time."""
    memo = session.info.setdefault("scheme_bands", {})
    hit = memo.get(scheme_key)
    if hit and time.monotonic() - hit[0] < BANDS_TTL_S:
        return hit[1], list(hit[2])
    scheme = session.scalar(select(GroupScheme).filter_by(key=scheme_key))
    if scheme is None:
        raise StatsError(f"unknown group scheme {scheme_key!r}")
    bands = [Band(b.key, b.label, b.lower, b.lower_unit, b.upper, b.upper_unit)
             for b in sorted(scheme.bands, key=lambda b: b.sort_order)]
    memo[scheme_key] = (time.monotonic(), scheme.parameter, bands)
    return scheme.parameter, list(bands)


class BandLookup:
    def __init__(self, session: Session, scheme_key: str, parameter: str):
        param, self.bands = load_bands(session, scheme_key)
        if param != parameter:
            raise StatsError(f"scheme {scheme_key!r} groups by {param}, not {parameter}")
        self.parameter = parameter
        self.order = {b.key: i for i, b in enumerate(self.bands)}
        self.label = {b.key: b.label for b in self.bands}
        self._memo: dict = {}

    def __call__(self, value) -> str | None:
        if value is None:
            return None
        if value in self._memo:
            return self._memo[value]
        if self.parameter == "age":
            b = band_for_age(self.bands, days=int(value))
        else:
            b = band_for_weight(self.bands, float(value)) if value > 0 else None
        self._memo[value] = b.key if b else None
        return self._memo[value]


# ------------------------------------------------------------------ main query


def compute(session: Session, conds: list, metric: str, group_by: list[str], *,
            system: str | None = None, exams: list[str] | None = None,
            age_scheme: str = "ro-drl-age", weight_scheme: str = "ext-body-weight",
            bins: int = 0, min_n: int = 1, physical: dict[int, int] | None = None,
            names: dict[int, str] | None = None, limit_rows: int = 3_000_000,
            estimated_weights: bool = True) -> dict:
    """Aggregate ``metric`` over the studies matching ``conds`` (SQL conditions on Study).

    ``estimated_weights=False``: weights estimated from the growth charts count as unknown
    in the weight groups (``weight_band``, ``ref_group``)."""
    if metric not in METRICS:
        raise StatsError(f"unknown metric {metric!r}; one of {', '.join(METRICS)}")
    bad = [g for g in group_by if g not in DIMENSIONS]
    if bad:
        raise StatsError(f"unknown dimension(s) {', '.join(bad)}; one of {', '.join(DIMENSIONS)}")
    m = METRICS[metric]
    on_events = m.level == "event"
    # ref_group: head exams by age, body exams by weight (grid.py); needs the exam code
    need_exam = "exam" in group_by or "ref_group" in group_by or bool(exams)

    cols = [m.column.label("value"), Study.id.label("sid"), Study.study_datetime,
            Study.device_id, Study.modality, Study.age_days,
            (Study.weight_kg if estimated_weights else case(
                (Study.weight_source == SizeSource.estimated_reference, None),
                else_=Study.weight_kg)).label("weight_kg")]
    q = select(*cols).select_from(Study)
    if on_events:
        q = q.join(Event, Event.study_id == Study.id).where(Event.is_localizer.is_not(True))
        q = q.add_columns(Event.protocol, Event.view)
    if "sex" in group_by:
        q = q.outerjoin(Patient, Patient.id == Study.patient_id).add_columns(Patient.sex)
    if "indication" in group_by:
        from .indications import USED

        si, ic = aliased(StudyIndication), aliased(Code)
        q = (q.outerjoin(si, and_(si.study_id == Study.id, si.status.in_(USED)))
             .outerjoin(ic, ic.id == si.code_id)
             .add_columns(ic.code.label("ind_code"), ic.meaning.label("ind_meaning")))
    if need_exam:
        system = system or "ro-nom-rx"
        codes = select(Code.id).join(CodeSystem).where(CodeSystem.key == system)
        if exams:
            codes = codes.where(Code.code.in_(exams))
        c = aliased(Classification)
        if on_events:
            # radiography exposures carry their own exam (with the projection); CT series
            # and fluoroscopy events take the exam of their study
            link = and_(c.study_id == Study.id,
                        or_(c.event_id == Event.id,
                            and_(c.event_id.is_(None), Study.modality.not_in(EXPOSURE_EXAM),
                                 ~own_codes(Event.id))))
        else:
            link = and_(c.study_id == Study.id, c.event_id.is_(None))
        cc = aliased(Code)
        join = q.join if exams else q.outerjoin
        q = join(c, and_(link, c.code_id.in_(codes)))
        q = q.outerjoin(cc, cc.id == c.code_id).add_columns(cc.code.label("exam"),
                                                            cc.meaning.label("exam_meaning"))
    q = q.where(m.column.is_not(None), *conds).limit(limit_rows)

    age = BandLookup(session, age_scheme, "age") if "age_band" in group_by else None
    weight = BandLookup(session, weight_scheme, "weight") if "weight_band" in group_by else None
    grid = None
    if "ref_group" in group_by:
        from .grid import Grid

        grid = Grid(session)
    if "device" in group_by and (physical is None or names is None):
        physical = physical or {i: (mm or i) for i, mm in
                                session.execute(select(Device.id, Device.merged_into_id))}
        names = names or {d.id: d.display_name or d.station_name or f"Device {d.id}"
                          for d in session.scalars(select(Device))}

    groups: dict[tuple, list[float]] = defaultdict(list)
    studies: dict[tuple, set[int]] = defaultdict(set)
    labels: dict[str, dict] = defaultdict(dict)
    all_values: list[float] = []
    truncated = False
    n_rows = 0
    for r in session.execute(q):
        n_rows += 1
        key = []
        for g in group_by:
            if g == "ref_group":
                key.append(grid.group(system, r.exam, r.age_days, r.weight_kg))
                labels[g][key[-1]] = grid.labels[key[-1]]
                continue
            key.append(_dim(g, r, age, weight, physical, names, labels))
        k = tuple(key)
        groups[k].append(r.value)
        studies[k].add(r.sid)
        all_values.append(r.value)
    if n_rows >= limit_rows:
        truncated = True

    edges = _edges(all_values, bins) if bins else None
    out_groups = []
    for k, vals in groups.items():
        d = describe(vals)
        if d["n"] < min_n:
            continue
        item = {"key": dict(zip(group_by, k, strict=True)),
                "label": {g: labels[g].get(v, v) for g, v in zip(group_by, k, strict=True)},
                "n_studies": len(studies[k]), **d}
        if edges:
            item["hist"] = histogram(vals, edges)
        out_groups.append(item)
    out_groups.sort(key=lambda it: _sort_key(it, group_by, age, weight, grid))
    return {
        "metric": metric, "unit": m.unit, "label": m.label, "level": m.level,
        "group_by": group_by, "system": system if need_exam else None,
        "age_scheme": age_scheme if age else None,
        "weight_scheme": weight_scheme if weight else None,
        "n": len(all_values), "n_studies": len({s for v in studies.values() for s in v}),
        "overall": describe(all_values), "bins": edges, "truncated": truncated,
        "groups": out_groups[:MAX_GROUPS], "groups_total": len(out_groups),
        # band labels in their natural order, for legends and axes
        "orders": {**({"age_band": [b.label for b in age.bands]} if age else {}),
                   **({"weight_band": [b.label for b in weight.bands]} if weight else {}),
                   **({"ref_group": grid.ordered_labels()} if grid else {})},
    }


def _dim(g, r, age, weight, physical, names, labels):
    if g == "exam":
        if r.exam is not None:
            labels["exam"][r.exam] = r.exam_meaning
        return r.exam
    if g == "device":
        did = physical.get(r.device_id, r.device_id) if r.device_id is not None else None
        if did is not None:
            labels["device"][did] = names.get(did, f"Device {did}")
        return did
    if g == "age_band":
        b = age(r.age_days)
        if b:
            labels["age_band"][b] = age.label[b]
        return b
    if g == "weight_band":
        b = weight(r.weight_kg)
        if b:
            labels["weight_band"][b] = weight.label[b]
        return b
    t: dt.datetime | None = r.study_datetime
    if g == "month":
        return t.strftime("%Y-%m") if t else None
    if g == "quarter":
        return f"{t.year}-Q{(t.month - 1) // 3 + 1}" if t else None
    if g == "year":
        return t.year if t else None
    if g == "modality":
        return r.modality
    if g == "sex":
        return r.sex
    if g == "indication":
        if r.ind_code is not None:
            labels["indication"][r.ind_code] = r.ind_meaning
        return r.ind_code
    if g == "protocol":
        return r.protocol
    if g == "view":
        from .classify import view_code

        return view_code(r.view, r.protocol)
    return None


def _edges(values: list[float], bins: int) -> list[float] | None:
    if not values:
        return None
    v = sorted(values)
    lo, hi = quantile(v, 0.01), quantile(v, 0.99)
    if hi <= lo:
        lo, hi = v[0], v[-1]
    if hi <= lo:
        hi = lo + 1
    step = (hi - lo) / bins
    return [lo + i * step for i in range(bins + 1)]


def _sort_key(item, group_by, age, weight, grid=None):
    out = []
    for g in group_by:
        v = item["key"][g]
        if g == "age_band" and age:
            out.append((0, age.order.get(v, 99), ""))
        elif g == "weight_band" and weight:
            out.append((0, weight.order.get(v, 99), ""))
        elif g == "ref_group" and grid:
            out.append((0, grid.order.get(v, 999), ""))
        elif v is None:
            out.append((1, 0, ""))
        elif isinstance(v, (int, float)):
            out.append((0, v, ""))
        else:
            out.append((0, 0, str(v)))
    out.append((0, -item["n"], ""))
    return out
