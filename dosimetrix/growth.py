"""Weight-for-age: z-scores and percentiles, for plausibility checks and DRL exclusions.

Tables (downloaded at setup, never bundled: each has its own terms):
* WHO Child Growth Standards (2006), weight-for-age, 0–5 years, by day (``weianthro.txt``);
* WHO Growth Reference (2007), weight-for-age, 5–10 years, by month (``wfawho2007.txt``);
* CDC 2000 growth charts, weight-for-age, 2–20 years, by half month (``wtage.csv``), used
  only beyond 10 years: WHO publishes no weight-for-age reference there. Optional: without
  it, patients over 10 years get no percentile.

z-scores use the LMS method with WHO's restricted extension beyond ±3 SD (WHO 2006
technical report, chapter 7): weight-for-age is skewed, so beyond +3/-3 SD the distance
between the 2 and 3 SD lines is used as the unit.

Use:
* data quality: a weight outside P1–P99 for age and sex is implausible (survey plan check);
* DRL statistics: patients below P5 or above P95 are excluded (``study.drl_exclusion``),
  as the national paediatric survey plan asks.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SOURCES = {
    "weianthro.txt": "https://raw.githubusercontent.com/WorldHealthOrganization/anthro/"
                     "master/data-raw/growthstandards/weianthro.txt",
    "wfawho2007.txt": "https://raw.githubusercontent.com/WorldHealthOrganization/anthroplus/"
                      "main/data-raw/growthstandards/wfawho2007.txt",
    "wtage.csv": "https://www.cdc.gov/growthcharts/data/zscore/wtage.csv",
}
NOTICE = """WHO weight-for-age tables, downloaded by `dosimetrix growth install` on {date}.

weianthro.txt  WHO Child Growth Standards (2006), 0-5 years, by day. Source:
               {weianthro}
wfawho2007.txt WHO Growth Reference (2007), 5-10 years, by month. Source:
               {wfawho2007}
wtage.csv      CDC 2000 growth charts, 2-20 years, by half month; used beyond 10 years
               only. Source: {wtage}

The WHO data are published by WHO in its GitHub repositories (WorldHealthOrganization/
anthro and /anthroplus); the CDC data by the US Centers for Disease Control and
Prevention. They are not part of Dosimetrix and are not redistributed with it; check the
publishers' terms before redistributing them.
"""
DAYS_PER_MONTH = 30.4375
CHILD_DAYS = 6575  # 18 years: DRL weight exclusions apply to children only
EXCLUDE_BELOW, EXCLUDE_ABOVE = 5.0, 95.0  # DRL statistics (survey plan)
IMPLAUSIBLE_BELOW, IMPLAUSIBLE_ABOVE = 1.0, 99.0  # data quality (survey plan)
# A weight whose source is known (measured, request form, HIS record) is kept unless it is
# physiologically impossible: |z| > 5. Between P1/P99 and that it is real but unusual (an
# ex-preterm infant, an obese teenager): kept, flagged by data quality, excluded from DRLs.
REALISTIC_Z = 5.0


@dataclass(frozen=True)
class LMS:
    l: float  # noqa: E741 – the conventional name
    m: float
    s: float

    def value(self, z: float) -> float:
        return self.m * (1 + self.l * self.s * z) ** (1 / self.l) if self.l else \
            self.m * math.exp(self.s * z)

    def z(self, x: float) -> float:
        """WHO restricted LMS z-score (adjusted beyond ±3 SD)."""
        z = (((x / self.m) ** self.l - 1) / (self.l * self.s)) if self.l else \
            math.log(x / self.m) / self.s
        if z > 3:
            sd3, sd2 = self.value(3), self.value(2)
            z = 3 + (x - sd3) / (sd3 - sd2)
        elif z < -3:
            sd3, sd2 = self.value(-3), self.value(-2)
            z = -3 - (sd3 - x) / (sd2 - sd3)
        return z


def percentile(z: float) -> float:
    return 50 * (1 + math.erf(z / math.sqrt(2)))


def _read(path: Path) -> dict[tuple[int, int], LMS]:
    out = {}
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh, delimiter="\t"):
            out[(int(r["sex"]), int(float(r["age"])))] = LMS(float(r["l"]), float(r["m"]),
                                                              float(r["s"]))
    return out


def _read_cdc(path: Path) -> dict[int, list[tuple[float, LMS]]]:
    """CDC weight-for-age: sex -> [(age in months, LMS)] sorted by age."""
    out: dict[int, list[tuple[float, LMS]]] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if not (r.get("Sex") or "").strip().isdigit():
                continue  # the file repeats its header line
            out.setdefault(int(r["Sex"]), []).append(
                (float(r["Agemos"]), LMS(float(r["L"]), float(r["M"]), float(r["S"]))))
    for rows in out.values():
        rows.sort(key=lambda x: x[0])
    return out


def _interp(rows: list[tuple[float, LMS]], months: float) -> LMS | None:
    if not rows or not rows[0][0] <= months <= rows[-1][0]:
        return None
    for (a0, x), (a1, y) in zip(rows, rows[1:], strict=False):
        if a0 <= months <= a1:
            f = (months - a0) / (a1 - a0) if a1 > a0 else 0.0
            return LMS(x.l + f * (y.l - x.l), x.m + f * (y.m - x.m), x.s + f * (y.s - x.s))
    return rows[-1][1]


class GrowthTables:
    def __init__(self, by_day: dict, by_month: dict, cdc: dict | None = None):
        self.by_day, self.by_month, self.cdc = by_day, by_month, cdc or {}
        self.max_day = max(a for _, a in by_day)
        self.month_range = (min(a for _, a in by_month), max(a for _, a in by_month))

    @classmethod
    def load(cls, directory: Path) -> GrowthTables | None:
        d, m = directory / "weianthro.txt", directory / "wfawho2007.txt"
        c = directory / "wtage.csv"
        if not (d.is_file() and m.is_file()):
            return None
        return cls(_read(d), _read(m), _read_cdc(c) if c.is_file() else None)

    @property
    def max_months(self) -> float:
        """Oldest age covered (months): 240 with the CDC table, else 120 (WHO)."""
        cdc = [rows[-1][0] for rows in self.cdc.values() if rows]
        return min(cdc) if cdc else self.month_range[1]

    def lms(self, sex: str | None, age_days: int | None) -> LMS | None:
        code = {"M": 1, "F": 2}.get((sex or "").upper())
        if code is None or age_days is None or age_days < 0:
            return None
        if age_days <= self.max_day:
            return self.by_day.get((code, age_days))
        months = age_days / DAYS_PER_MONTH
        lo_m, hi_m = self.month_range
        if months > hi_m:  # no WHO weight-for-age beyond 10 years: CDC 2000, if installed
            return _interp(self.cdc.get(code, []), months)
        lo = max(int(months), lo_m)
        hi = min(lo + 1, hi_m)
        a, b = self.by_month.get((code, lo)), self.by_month.get((code, hi))
        if a is None or b is None:
            return None
        f = months - lo if hi > lo else 0.0
        return LMS(a.l + f * (b.l - a.l), a.m + f * (b.m - a.m), a.s + f * (b.s - a.s))

    def assess(self, sex: str | None, age_days: int | None, weight_kg: float | None
               ) -> dict | None:
        """{'z', 'percentile', 'exclusion' (DRL), 'implausible'} or None (not assessable)."""
        if not weight_kg or weight_kg <= 0:
            return None
        lms = self.lms(sex, age_days)
        if lms is None:
            return None
        z = lms.z(weight_kg)
        p = percentile(z)
        exclusion = ("weight < P5 for age" if p < EXCLUDE_BELOW
                     else "weight > P95 for age" if p > EXCLUDE_ABOVE else None)
        source = "CDC" if age_days > self.max_day and \
            age_days / DAYS_PER_MONTH > self.month_range[1] else "WHO"
        return {"z": round(z, 2), "percentile": round(p, 1), "exclusion": exclusion,
                "source": source,
                "implausible": p < IMPLAUSIBLE_BELOW or p > IMPLAUSIBLE_ABOVE,
                "unrealistic": abs(z) > REALISTIC_Z}


_CACHE: dict[Path, GrowthTables | None] = {}


def tables(directory: Path | None = None) -> GrowthTables | None:
    """The installed tables (cached per process), or None when not installed."""
    if directory is None:
        from .config import load_settings

        directory = load_settings().data_dir / "growth"
    directory = Path(directory)
    if directory not in _CACHE:
        _CACHE[directory] = GrowthTables.load(directory)
    return _CACHE[directory]


def install(directory: Path, sources: dict[str, str] = SOURCES, timeout: float = 60) -> list[str]:
    """Download the tables; validate them before replacing anything."""
    directory.mkdir(parents=True, exist_ok=True)
    report = []
    for name, url in sources.items():
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 – fixed URLs
            body = r.read()
        tmp = directory / f".{name}.part"
        tmp.write_bytes(body)
        rows = _read_cdc(tmp) if name.endswith(".csv") else _read(tmp)
        n = sum(len(v) for v in rows.values()) if name.endswith(".csv") else len(rows)
        sexes = set(rows) if name.endswith(".csv") else {s for s, _ in rows}
        if n < 100 or sexes != {1, 2}:
            tmp.unlink()
            raise ValueError(f"{name}: unexpected content from {url}")
        tmp.replace(directory / name)
        report.append(f"{name}: {n} rows")
    (directory / "SOURCE.txt").write_text(NOTICE.format(
        date=dt.date.today().isoformat(), weianthro=sources["weianthro.txt"],
        wfawho2007=sources["wfawho2007.txt"], wtage=sources.get("wtage.csv", "-")),
        encoding="utf-8")
    _CACHE.pop(directory, None)
    return report


def apply_exclusions(session, study_ids=None) -> dict:
    """Set ``study.drl_exclusion`` from the weight percentile (only touches weight-based
    reasons; other exclusion reasons set by people are kept)."""
    from sqlalchemy import select

    from .db.models import Patient, Study

    g = tables()
    stats = {"checked": 0, "excluded": 0, "cleared": 0, "not_assessable": 0}
    if g is None:
        return {**stats, "error": "growth tables not installed (dosimetrix growth install)"}
    q = (select(Study, Patient.sex).outerjoin(Patient, Patient.id == Study.patient_id)
         .where(Study.weight_kg.is_not(None), Study.age_days < CHILD_DAYS))
    if study_ids is not None:
        q = q.where(Study.id.in_(list(study_ids)))
    for s, sex in session.execute(q):
        stats["checked"] += 1
        a = g.assess(sex, s.age_days, s.weight_kg)
        ours = (s.drl_exclusion or "").startswith("weight ")
        if a is None:
            stats["not_assessable"] += 1
            continue
        if a["exclusion"] and (s.drl_exclusion is None or ours):
            if s.drl_exclusion != a["exclusion"]:
                s.drl_exclusion = a["exclusion"]
            stats["excluded"] += 1
        elif not a["exclusion"] and ours:
            s.drl_exclusion = None
            stats["cleared"] += 1
    session.flush()
    return stats


def estimate_weights(session, study_ids=None, batch: int = 5000) -> dict:
    """Missing or implausible weights -> the growth-reference median for age and sex.

    * no weight: ``weight_kg`` = median (P50) of the table for the patient's age and sex,
      ``weight_source`` = ``estimated_reference``;
    * a reported weight outside P1–P99: kept in ``weight_reported_kg``, replaced the same
      way (the quality rule ``weight_implausible`` still reports it); a weight with a known
      source (measured, request form, HIS record) only when unrealistic (|z| > 5);
    * an earlier estimate is refreshed (e.g. after a table update);
    * needs age, sex M/F and an age the tables cover (0–20 years with the CDC table;
      0–10 years with WHO only). Others keep their weight, or none.

    The estimate is the protocol's lowest-priority weight source: it puts body exams in a
    weight group, and is always marked as estimated (study page, exports, reports).
    """
    from sqlalchemy import select, update

    from .db.models import Patient, SizeSource, Study

    g = tables()
    stats = {"estimated_missing": 0, "replaced_implausible": 0, "refreshed": 0}
    if g is None:
        return {**stats, "error": "growth tables not installed (dosimetrix growth install)"}
    est_src = SizeSource.estimated_reference
    TRUSTED = {SizeSource.measured, SizeSource.chart_same_day, SizeSource.chart_72h,  # noqa: N806
               SizeSource.his_chart_undated}
    q = (select(Study.id, Study.age_days, Study.weight_kg, Study.weight_source, Patient.sex)
         .join(Patient, Patient.id == Study.patient_id)
         .where(Study.age_days.is_not(None), Patient.sex.in_(["M", "F", "m", "f"]),
                Study.age_days <= int(g.max_months * DAYS_PER_MONTH)))
    if study_ids is not None:
        q = q.where(Study.id.in_(list(study_ids)))
    changes = []
    for sid, age, w, src, sex in session.execute(q):
        lms = g.lms(sex, age)
        if lms is None:
            continue
        est = round(lms.m, 1)
        if src == est_src:
            if w != est:
                changes.append({"id": sid, "weight_kg": est})
                stats["refreshed"] += 1
            continue
        if not w or w <= 0:
            changes.append({"id": sid, "weight_kg": est, "weight_source": est_src})
            stats["estimated_missing"] += 1
            continue
        a = g.assess(sex, age, w)
        if a is not None and (a["unrealistic"] if src in TRUSTED else a["implausible"]):
            changes.append({"id": sid, "weight_kg": est, "weight_source": est_src,
                            "weight_reported_kg": w})
            stats["replaced_implausible"] += 1
    for i in range(0, len(changes), batch):
        part = changes[i:i + batch]
        for keys in {tuple(sorted(c)) for c in part}:  # one statement per column set
            rows = [c for c in part if tuple(sorted(c)) == keys]
            session.execute(update(Study), rows)
    session.flush()
    return stats
