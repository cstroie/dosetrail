"""Protocol comparison: the same CT region scanned under many protocol names.

A site has several scanners and many external studies; the same region (CT head) goes by
NATIV, CEREBRAL, CERRBRUM, CAP... and the protocols differ in kV, mA, pitch, collimation and
so in dose. This module puts them side by side so the distribution and the lowest dose can
be seen.

* **Region** = one exam code (default a national DRL code such as ``CT-CAP``, which
  gathers head, low-dose head and head trauma; a nomenclator code works too). Series are
  linked to it as the DRL comparison does (``drl.collect``): the series' own code, or the
  study's.
* **Patient groups** follow ICRP 135 / RP 185 (``grid="icrp"``: head by age, body by
  weight; see grid.py) or the national age groups. A patient without age / weight falls
  back to the band written in the protocol name (``CT_CAP_N_1_6_ANI``: 1–6 years;
  ``..._15_30KG``), when it has one; the row counts how many series that applies to and how
  many names disagree with the patient.
* **Unit of comparison** = group × protocol × (room) × phantom (a head series on the 32 cm
  phantom reads about half the 16 cm CTDIvol, so the phantoms are never mixed). ``by``
  ``protocol`` keeps each normalised name; ``family`` merges the contrast / phase / band
  variants of a name (``CER_NATIV``, ``CER K``, ``CEREBRAL C``).
* Not diagnostic acquisitions are left out: bolus-tracking monitoring (stationary series
  or scan length under 20 mm), aborted scans, series without dose.
* Per row: n and the distribution (P25, median, P75, min, max) of CTDIvol, DLP, kVp, mA,
  rotation time, pitch, collimation, scan length and the effective mAs (mA × rotation /
  pitch), plus a CTDIvol histogram on the group's own edges.
* **Dose rank** within group and phantom, among rows with at least ``min_n`` series: by the
  median CTDIvol. The lowest dose is not the best protocol by itself: image quality (noise,
  slice thickness, reconstruction) is not in the dose data, so the parameters are shown to
  let a radiologist judge; slice thickness is only present for header-sourced series.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session, aliased

from .classify import normalise
from .db.models import Classification, Code, CodeSystem, Event, SizeSource, Study
from .stats import EXPOSURE_EXAM, BandLookup, _edges, describe, histogram, own_codes

NATIONAL = "ro-drl-ms838-2025"
NATIONAL_SCHEME = "ro-drl-age"
METRICS = {"ctdivol": "mGy", "dlp": "mGy.cm", "kvp": "kV", "ma": "mA", "rotation_s": "s",
           "pitch": "", "collimation_mm": "mm", "scan_length_mm": "mm", "emas": "mAs"}
BINS = 12
DAYS_PER_YEAR = 365.25

# "1 6 ani" (years), "3 12 luni" (months), "15 30 kg" in normalised text
_AGE_Y = re.compile(r" (\d{1,2}) (\d{1,2}) (?:ani|an|years?|yrs?|y) ")
_AGE_M = re.compile(r" (\d{1,2}) (\d{1,2}) (?:luni|lun|months?|mo|m) ")
_WEIGHT = re.compile(r" (\d{1,3}) (\d{1,3}) ?kg ")
_WEIGHT_ONE = re.compile(r" (\d{1,3}) ?kg ")
# words that only say contrast / phase / the band, not the protocol
_NOISE = {"n", "c", "k", "v", "sc", "cc", "nativ", "native", "nat", "contrast", "cont", "art",
          "arterial", "ven", "venos", "tardiv", "tard", "ani", "an", "kg", "adult", "child",
          "ped", "pediatric"}


# Series that are not diagnostic acquisitions: bolus-tracking monitoring (stationary, ~10 mm),
# aborted scans. A bolus name alone does not exclude: "T/A/P BOLUS" is often the real scan.
_NOT_DIAGNOSTIC = re.compile(r"monitor|abort|test bolus|smart ?prep|care ?bolus")
MIN_SCAN_MM = 20.0


class ProtocolsError(ValueError):
    pass


def name_band(protocol: str | None) -> tuple[str, float] | None:
    """The patient group a protocol name states: ("age", days) or ("weight", kg), the middle
    of the range written in the name (``CT_CAP_N_1_6_ANI`` -> age 3.5 years)."""
    t = normalise(protocol)
    if not t:
        return None
    if m := _AGE_Y.search(t):
        return "age", (int(m[1]) + int(m[2])) / 2 * DAYS_PER_YEAR
    if m := _AGE_M.search(t):
        return "age", (int(m[1]) + int(m[2])) / 2 * DAYS_PER_YEAR / 12
    if m := _WEIGHT.search(t):
        return "weight", (int(m[1]) + int(m[2])) / 2
    if m := _WEIGHT_ONE.search(t):
        return "weight", float(m[1])
    return None


def family(protocol: str | None) -> str:
    """The protocol name without contrast / phase / band words and numbers: variants of one
    protocol (``CER_NATIV``, ``CER K``) share it."""
    t = _AGE_Y.sub(" ", _AGE_M.sub(" ", _WEIGHT.sub(" ", normalise(protocol))))
    words = [w for w in t.split() if w not in _NOISE and not w.isdigit()]
    return " ".join(words) or normalise(protocol).strip()


def _emas(ma, rot, pitch):
    return ma * rot / pitch if ma and rot and pitch else None


def compare(session: Session, conds: list, region: str, *, system: str = NATIONAL,
            grid: str = "icrp", by: str = "protocol", by_device: bool = True, min_n: int = 10,
            estimated_weights: bool = False) -> dict:
    """The protocols of one CT region in the studies ``conds`` selects (put the period in
    conds), per patient group."""
    from .api.data import device_names
    from .grid import Grid

    if by not in ("protocol", "family"):
        raise ProtocolsError("by is protocol or family")
    if grid not in ("icrp", "national"):
        raise ProtocolsError("grid is icrp or national")
    code = session.scalar(select(Code).join(CodeSystem).where(CodeSystem.key == system,
                                                              Code.code == region))
    if code is None:
        raise ProtocolsError(f"unknown exam {region!r} in {system}")
    if grid == "icrp":
        g = Grid(session)
        param = g.parameter(system, region)
        labels = g.labels
        order = g.order

        def band_of(age, weight):
            return g.group(system, region, age, weight)

        def name_key(kind, value):  # only a band of the exam's own parameter counts
            if kind != param:
                return None
            b = g.age(int(value)) if kind == "age" else g.weight(value)
            return f"{kind}:{b}" if b else None

    else:
        lk = BandLookup(session, NATIONAL_SCHEME, "age")
        param = "age"
        labels = {f"age:{b.key}": b.label for b in lk.bands}
        labels["age:?"] = "Age unknown"
        order = {f"age:{b.key}": i for i, b in enumerate(lk.bands)}
        order["age:?"] = 50

        def band_of(age, weight):
            b = lk(age)
            return f"age:{b}" if b else "age:?"

        def name_key(kind, value):
            if kind != "age":
                return None
            b = lk(int(value))
            return f"age:{b}" if b else None

    c = aliased(Classification)
    weight = Study.weight_kg if estimated_weights else case(
        (Study.weight_source == SizeSource.estimated_reference, None), else_=Study.weight_kg)
    link = and_(c.study_id == Study.id, c.code_id == code.id,
                or_(c.event_id == Event.id,
                    and_(c.event_id.is_(None), Study.modality.not_in(EXPOSURE_EXAM),
                         ~own_codes(Event.id))))
    q = (select(Event.protocol, Event.ctdivol, Event.dlp, Event.kvp, Event.ma,
                Event.rotation_time_s, Event.pitch, Event.collimation_mm,
                Event.scan_length_mm, Event.ct_phantom_cm, Study.device_id, Study.age_days,
                weight, Study.id)
         .select_from(Event).join(Study, Study.id == Event.study_id).join(c, link)
         .where(Event.is_localizer.is_not(True), Event.ctdivol.is_not(None),
                Event.dlp > 0, or_(Event.event_type.is_(None), Event.event_type != "stationary"),
                or_(Event.scan_length_mm.is_(None), Event.scan_length_mm >= MIN_SCAN_MM),
                Study.drl_exclusion.is_(None), *conds))
    physical = None
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "raw": Counter(), "vals": defaultdict(list), "studies": set(), "from_name": 0,
        "mismatch": 0})
    for (protocol, ctdivol, dlp, kvp, ma, rot, pitch, coll, length, phantom, dev, age, wt,
         sid) in session.execute(q):
        if physical is None:
            from .db.models import Device
            physical = {i: (m or i) for i, m in session.execute(
                select(Device.id, Device.merged_into_id))}
        if _NOT_DIAGNOSTIC.search(normalise(protocol)):
            continue
        band = band_of(age, wt)
        nb = name_band(protocol)
        named = name_key(*nb) if nb else None
        from_name = False
        if band.endswith(":?") and named:
            band, from_name = named, True
        mismatch = bool(named and not band.endswith(":?") and not from_name and named != band)
        name = normalise(protocol).strip() or "(no name)"
        key = (band, phantom, (physical.get(dev, dev) if by_device and dev is not None
                               else None), family(protocol) if by == "family" else name)
        grp = groups[key]
        grp["raw"][protocol or "(no name)"] += 1
        grp["studies"].add(sid)
        grp["from_name"] += from_name
        grp["mismatch"] += mismatch
        for k, v in (("ctdivol", ctdivol), ("dlp", dlp), ("kvp", kvp), ("ma", ma),
                     ("rotation_s", rot), ("pitch", pitch), ("collimation_mm", coll),
                     ("scan_length_mm", length), ("emas", _emas(ma, rot, pitch))):
            if v is not None:
                grp["vals"][k].append(v)
    names = device_names(session)
    # one histogram scale per patient group and phantom: bars are comparable
    scale = {}
    for (band, phantom, _d, _p), grp in groups.items():
        scale.setdefault((band, phantom), []).extend(grp["vals"]["ctdivol"])
    edges = {k: _edges(v, BINS) for k, v in scale.items()}
    rows = []
    for (band, phantom, dev, name), grp in groups.items():
        stats = {k: describe(v) for k, v in grp["vals"].items()}
        n = stats["ctdivol"]["n"]
        rows.append({
            "band": band, "band_label": labels.get(band, band), "phantom_cm": phantom,
            "device": dev, "device_name": names.get(dev, f"Device {dev}") if dev else None,
            "protocol": name, "names": [k for k, _ in grp["raw"].most_common(5)],
            "n": n, "studies": len(grp["studies"]),
            "n_from_name": grp["from_name"], "n_name_mismatch": grp["mismatch"],
            "stats": stats,
            "hist": {"edges": edges[(band, phantom)],
                     "counts": histogram(grp["vals"]["ctdivol"], edges[(band, phantom)])
                     if edges[(band, phantom)] else []},
            "enough": n >= min_n})
    # dose rank within band and phantom, among rows with enough series
    by_group = defaultdict(list)
    for r in rows:
        if r["enough"]:
            by_group[(r["band"], r["phantom_cm"])].append(r)
    for rs in by_group.values():
        rs.sort(key=lambda r: r["stats"]["ctdivol"]["median"])
        low = rs[0]["stats"]["ctdivol"]["median"]
        for i, r in enumerate(rs, 1):
            r["rank"] = i
            r["ratio_to_lowest"] = (r["stats"]["ctdivol"]["median"] / low) if low else None
    for r in rows:
        r.setdefault("rank", None)
        r.setdefault("ratio_to_lowest", None)
    rows.sort(key=lambda r: (order.get(r["band"], 999), r["phantom_cm"] or 0,
                             r["rank"] or 10**6, -r["n"]))
    return {"region": region, "system": system, "meaning": code.meaning, "grid": grid,
            "parameter": param, "by": by, "by_device": by_device, "min_n": min_n,
            "metrics": METRICS, "rows": rows,
            "note": "Lowest dose is not best quality: noise and slice thickness are not in "
                    "the dose data; judge the parameters, not the dose alone."}
