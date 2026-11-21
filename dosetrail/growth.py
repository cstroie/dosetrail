"""WHO weight-for-age: z-scores and percentiles, for plausibility checks and DRL exclusions.

Tables (downloaded at setup, never bundled: the WHO data has its own licence):
* WHO Child Growth Standards (2006), weight-for-age, 0–5 years, by day (``weianthro.txt``);
* WHO Growth Reference (2007), weight-for-age, 5–10 years, by month (``wfawho2007.txt``).
WHO publishes no weight-for-age reference beyond 10 years, so older patients get no
percentile.

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
}
NOTICE = """WHO weight-for-age tables, downloaded by `dosetrail growth install` on {date}.

weianthro.txt  WHO Child Growth Standards (2006), 0-5 years, by day. Source:
               {weianthro}
wfawho2007.txt WHO Growth Reference (2007), 5-10 years, by month. Source:
               {wfawho2007}

These are WHO data, published by WHO in its GitHub repositories (WorldHealthOrganization/
anthro and /anthroplus). They are not part of DoseTrail and are not redistributed with it;
check WHO's terms of use before redistributing them.
"""
DAYS_PER_MONTH = 30.4375
EXCLUDE_BELOW, EXCLUDE_ABOVE = 5.0, 95.0  # DRL statistics (survey plan)
IMPLAUSIBLE_BELOW, IMPLAUSIBLE_ABOVE = 1.0, 99.0  # data quality (survey plan)


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


class GrowthTables:
    def __init__(self, by_day: dict, by_month: dict):
        self.by_day, self.by_month = by_day, by_month
        self.max_day = max(a for _, a in by_day)
        self.month_range = (min(a for _, a in by_month), max(a for _, a in by_month))

    @classmethod
    def load(cls, directory: Path) -> GrowthTables | None:
        d, m = directory / "weianthro.txt", directory / "wfawho2007.txt"
        if not (d.is_file() and m.is_file()):
            return None
        return cls(_read(d), _read(m))

    def lms(self, sex: str | None, age_days: int | None) -> LMS | None:
        code = {"M": 1, "F": 2}.get((sex or "").upper())
        if code is None or age_days is None or age_days < 0:
            return None
        if age_days <= self.max_day:
            return self.by_day.get((code, age_days))
        months = age_days / DAYS_PER_MONTH
        lo_m, hi_m = self.month_range
        if months > hi_m:
            return None  # no WHO weight-for-age beyond 10 years
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
        return {"z": round(z, 2), "percentile": round(p, 1), "exclusion": exclusion,
                "implausible": p < IMPLAUSIBLE_BELOW or p > IMPLAUSIBLE_ABOVE}


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
        rows = _read(tmp)
        if len(rows) < 100 or {s for s, _ in rows} != {1, 2}:
            tmp.unlink()
            raise ValueError(f"{name}: unexpected content from {url}")
        tmp.replace(directory / name)
        report.append(f"{name}: {len(rows)} rows")
    (directory / "SOURCE.txt").write_text(NOTICE.format(
        date=dt.date.today().isoformat(), weianthro=sources["weianthro.txt"],
        wfawho2007=sources["wfawho2007.txt"]), encoding="utf-8")
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
        return {**stats, "error": "growth tables not installed (dosetrail growth install)"}
    q = (select(Study, Patient.sex).outerjoin(Patient, Patient.id == Study.patient_id)
         .where(Study.weight_kg.is_not(None), Study.age_days < 3653))
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
