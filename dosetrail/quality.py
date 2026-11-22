"""Data-quality rules: per-study findings and per-device completeness.

Rules (severity ``error`` = the value is almost certainly wrong, ``warn`` = check it):
* ``ct_dlp_mismatch`` (warn): a CT series where CTDIvol × scan length differs from DLP by
  more than 10 % (the survey plan's check).
* ``phantom_32_head`` (warn): a paediatric head CT measured with the 32 cm phantom, which
  halves CTDIvol.
* ``dose_absurd`` (error): physically implausible values: CTDIvol > 300 mGy, a series
  DLP > 15,000 mGy·cm, an exposure DAP > 1,000 Gy·cm² (radiography 50), Ka,r > 20 Gy.
* ``dose_far_above_drl`` (warn): a study value above 3 × the national DRL of its exam and
  age group. Probably an error, or a case worth reviewing.
* ``weight_implausible`` (warn): outside weight-for-age P1–P99 (WHO to 10 years, CDC 2000
  to 20 years; when installed: ``dosetrail growth install``); otherwise wide bounds.
* ``dap_missing`` (warn): a radiography or fluoroscopy study with exposures but no DAP
  (the unit reported none, or 0: DAP meter absent, off or out of calibration).
* ``age_missing`` / ``sex_missing`` (warn).
* ``possible_duplicate`` (warn): another study of the same patient on the same device
  within 2 minutes, with a different Study UID.

Completeness KPIs per device: the share of studies with an exam code, age, measured or
charted weight, and sex.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import (
    Classification,
    Code,
    CodeSystem,
    Device,
    Event,
    Patient,
    ReferenceSet,
    ReferenceValue,
    SizeSource,
    Study,
)
from .stats import BandLookup

RULES = {
    "ct_dlp_mismatch": ("warn", "CTDIvol × scan length differs from DLP by more than 10 %"),
    "phantom_32_head": ("warn", "Paediatric head CT with the 32 cm phantom"),
    "dose_absurd": ("error", "Physically implausible dose value"),
    "dose_far_above_drl": ("warn", "Dose above 3 × the national DRL of the exam"),
    "weight_implausible": ("warn", "Weight implausible for the age"),
    "dap_missing": ("warn", "Exposures without DAP (none or 0 reported)"),
    "age_missing": ("warn", "No age"),
    "sex_missing": ("warn", "No sex"),
    "possible_duplicate": ("warn", "Another study of the patient on the device within 2 min"),
}
MEASURED = {SizeSource.measured, SizeSource.chart_same_day, SizeSource.chart_72h}
HEAD_EXAMS = {"CT-CAP", "CT-SINUS"}
ABSURD = {"ctdivol": 300.0, "dlp": 15000.0, "dap_rx": 50.0, "dap": 1000.0, "kar": 20.0}
BATCH = 2000


@dataclass
class Finding:
    rule: str
    detail: str

    @property
    def severity(self) -> str:
        return RULES[self.rule][0]

    def as_dict(self) -> dict:
        return {"rule": self.rule, "severity": self.severity, "title": RULES[self.rule][1],
                "detail": self.detail}


HEAD_SERIES = {"CT-CR", "CT-CRLD", "CT-CROR", "CT-CRMS", "CT-CRMF", "CT-CRPF", "CT-SN",
               "CT-SNLD", "CT-SNCO", "CT-TRCR"}


def _head_series(e: Event, study_exam: str | None) -> bool:
    """A head series: its own protocol says head; or it says nothing and the exam is head.
    Body series of a combined (e.g. trauma) study are legitimately measured at 32 cm."""
    from .classify import default_rules, normalise

    own = default_rules().match("ct", normalise(e.protocol))
    if own is not None:
        return own in HEAD_SERIES
    return study_exam in HEAD_EXAMS


def weight_bounds(age_days: int) -> tuple[float, float]:
    """Very wide physiological bounds by age (WHO tables will refine this)."""
    y = age_days / 365.25
    if y < 1:
        return 0.3, 15.0
    if y < 5:
        return 5.0, 40.0
    if y < 10:
        return 10.0, 90.0
    if y < 15:
        return 15.0, 150.0
    return 25.0, 250.0


class Checker:
    def __init__(self, session: Session):
        from . import growth

        self.growth = growth.tables()
        self.band = BandLookup(session, "ro-drl-age", "age")
        # (national exam code, band key) -> reference value in local units
        self.drl: dict[tuple[str, str | None, str], float] = {}
        rs = session.scalar(select(ReferenceSet).filter_by(key="ro-ms838-2025"))
        if rs is not None:
            from .db.models import GroupBand

            for rv, code, band in session.execute(
                    select(ReferenceValue, Code.code, GroupBand.key)
                    .join(Code, Code.id == ReferenceValue.exam_code_id)
                    .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
                    .where(ReferenceValue.set_id == rs.id)):
                if rv.quantity in ("dlp", "dap") and rv.unit in ("mGy.cm", "Gy.cm2"):
                    self.drl[(code, band, rv.quantity)] = rv.value

    def study(self, s: Study, p: Patient | None, events: list[Event],
              national: dict[int | None, str]) -> list[Finding]:
        """``national``: event id (or None for the study) -> national DRL exam code."""
        out: list[Finding] = []
        if s.age_days is None:
            out.append(Finding("age_missing", ""))
        if p is None or not p.sex or p.sex.upper() not in ("M", "F"):
            out.append(Finding("sex_missing", ""))
        if s.weight_reported_kg:  # replaced by an estimate (growth.estimate_weights)
            out.append(Finding("weight_implausible",
                               f"reported {s.weight_reported_kg:g} kg; estimate "
                               f"{s.weight_kg:g} kg used (median for age and sex)"))
        elif s.weight_kg and s.weight_source != SizeSource.estimated_reference:
            who = self.growth.assess(p.sex if p else None, s.age_days, s.weight_kg) \
                if self.growth else None
            if who is not None:  # weight-for-age (WHO, CDC beyond 10 y): outside P1-P99
                if who["implausible"]:
                    out.append(Finding("weight_implausible",
                                       f"{s.weight_kg:g} kg = P{who['percentile']:g} for age "
                                       f"({who['source']}, z {who['z']:+.1f})"))
            else:
                lo, hi = weight_bounds(s.age_days) if s.age_days is not None else (0.3, 250.0)
                if not lo <= s.weight_kg <= hi:
                    out.append(Finding("weight_implausible",
                                       f"{s.weight_kg:g} kg (expected {lo:g}–{hi:g} kg)"))
        band = self.band(s.age_days)
        rx = s.modality in ("DX", "CR", "MG")
        per_code: dict[str, float] = defaultdict(float)
        for e in events:
            if e.is_localizer:
                continue
            if s.modality == "CT":
                if e.ctdivol and e.scan_length_mm and e.dlp:
                    expected = e.ctdivol * e.scan_length_mm / 10
                    if abs(expected - e.dlp) > 0.1 * e.dlp:
                        out.append(Finding("ct_dlp_mismatch",
                                           f"{e.protocol or 'series'}: {e.ctdivol:g} mGy × "
                                           f"{e.scan_length_mm / 10:g} cm = {expected:.0f} vs "
                                           f"DLP {e.dlp:.0f} mGy·cm"))
                if (e.ct_phantom_cm == 32 and s.age_days is not None
                        and s.age_days < 18 * 365.25 and _head_series(e, national.get(None))):
                    out.append(Finding("phantom_32_head", e.protocol or "series"))
                if e.ctdivol and e.ctdivol > ABSURD["ctdivol"]:
                    out.append(Finding("dose_absurd", f"CTDIvol {e.ctdivol:g} mGy"))
                if e.dlp and e.dlp > ABSURD["dlp"]:
                    out.append(Finding("dose_absurd", f"DLP {e.dlp:g} mGy·cm"))
                code = national.get(e.id) or national.get(None)
                if code and e.dlp:
                    ref = self.drl.get((code, band, "dlp"))
                    if ref and e.dlp > 3 * ref:
                        out.append(Finding("dose_far_above_drl",
                                           f"{code} series DLP {e.dlp:.0f} > 3 × {ref:g}"))
            else:
                limit = ABSURD["dap_rx"] if rx else ABSURD["dap"]
                if e.dap and e.dap > limit:
                    out.append(Finding("dose_absurd", f"DAP {e.dap:g} Gy·cm²"))
                if e.rp_dose and e.rp_dose > ABSURD["kar"]:
                    out.append(Finding("dose_absurd", f"Ka,r {e.rp_dose:g} Gy"))
                code = national.get(e.id) if rx else national.get(None)
                if code and e.dap:
                    per_code[code] += e.dap
        if s.modality in ("DX", "CR", "RF", "XA") and events \
                and all(e.dap is None for e in events if not e.is_localizer):
            zero = sum(1 for e in events
                       if "dap" in ((e.extra_json or {}).get("reported_zero") or []))
            out.append(Finding("dap_missing", f"{len(events)} exposures"
                               + (f", {zero} reported as 0" if zero else "")))
        for code, dap in per_code.items():
            ref = self.drl.get((code, band, "dap"))
            if ref and dap > 3 * ref:
                out.append(Finding("dose_far_above_drl", f"{code} DAP {dap:.3g} > 3 × {ref:g}"))
        return out


def _national_codes(session: Session, ids: list[int]) -> dict[int, dict[int | None, str]]:
    out: dict[int, dict[int | None, str]] = defaultdict(dict)
    for sid, eid, code in session.execute(
            select(Classification.study_id, Classification.event_id, Code.code)
            .join(Code, Code.id == Classification.code_id).join(CodeSystem)
            .where(CodeSystem.key == "ro-drl-ms838-2025", Classification.study_id.in_(ids))):
        out[sid][eid] = code
    return out


def study_findings(session: Session, study_id: int) -> list[dict]:
    row = session.execute(select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
                          .where(Study.id == study_id)).first()
    if row is None:
        return []
    s, p = row
    events = list(session.scalars(select(Event).where(Event.study_id == s.id)))
    found = Checker(session).study(s, p, events, _national_codes(session, [s.id]).get(s.id, {}))
    dup = _duplicates(session, [s])
    found += [Finding("possible_duplicate", f"study {o}") for o in dup.get(s.id, [])]
    return [f.as_dict() for f in found]


def _duplicates(session: Session, studies: list[Study]) -> dict[int, list[int]]:
    """Studies of the same patient on the same device within 2 minutes."""
    keyed = [(s.patient_id, s.device_id, s.study_datetime, s.id) for s in studies
             if s.patient_id and s.device_id and s.study_datetime]
    if not keyed:
        return {}
    pats = {k[0] for k in keyed}
    lo = min(k[2] for k in keyed) - dt.timedelta(minutes=2)
    hi = max(k[2] for k in keyed) + dt.timedelta(minutes=2)
    near: dict[tuple, list[tuple]] = defaultdict(list)
    for pid, did, t, sid in session.execute(
            select(Study.patient_id, Study.device_id, Study.study_datetime, Study.id)
            .where(Study.patient_id.in_(pats), Study.study_datetime.between(lo, hi))):
        near[(pid, did)].append((t, sid))
    out: dict[int, list[int]] = defaultdict(list)
    for pid, did, t, sid in keyed:
        for t2, sid2 in near.get((pid, did), []):
            if sid2 != sid and abs((t2 - t).total_seconds()) <= 120:
                out[sid].append(sid2)
    return out


def run(session: Session, conds: list, flagged_limit: int = 200,
        names: dict[int, str] | None = None, physical: dict[int, int] | None = None) -> dict:
    """Rules over the studies the query selects: counts per rule and device, completeness,
    and the most recent flagged studies."""
    checker = Checker(session)
    physical = physical or {i: (m or i) for i, m in
                            session.execute(select(Device.id, Device.merged_into_id))}
    names = names or {d.id: d.display_name or f"Device {d.id}"
                      for d in session.scalars(select(Device))}
    ids = list(session.scalars(select(Study.id).where(*conds)
                               .order_by(Study.study_datetime.desc(), Study.id.desc())))
    rule_counts: Counter = Counter()
    per_device: dict[int | None, dict] = {}
    flagged: list[dict] = []
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = session.execute(
            select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.id.in_(chunk))).all()
        events: dict[int, list[Event]] = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_(chunk))):
            events[e.study_id].append(e)
        national = _national_codes(session, chunk)
        coded = set(session.scalars(select(Classification.study_id).where(
            Classification.event_id.is_(None), Classification.study_id.in_(chunk))))
        dups = _duplicates(session, [s for s, _ in studies])
        for s, p in sorted(studies, key=lambda x: x[0].study_datetime or dt.datetime.min,
                           reverse=True):
            dev = physical.get(s.device_id, s.device_id) if s.device_id else None
            d = per_device.setdefault(dev, {"device": dev, "device_name": names.get(dev),
                                            "studies": 0, "exam_code": 0, "age": 0,
                                            "weight": 0, "weight_measured": 0, "sex": 0,
                                            "flagged": 0, "rules": Counter()})
            d["studies"] += 1
            d["exam_code"] += s.id in coded
            d["age"] += s.age_days is not None
            d["weight"] += bool(s.weight_kg)
            d["weight_measured"] += bool(s.weight_kg) and s.weight_source in MEASURED
            d["sex"] += bool(p and p.sex and p.sex.upper() in ("M", "F"))
            found = checker.study(s, p, events.get(s.id, []), national.get(s.id, {}))
            found += [Finding("possible_duplicate", f"study {o}") for o in dups.get(s.id, [])]
            rules = {f.rule for f in found} - {"age_missing", "sex_missing"}
            for f in found:
                rule_counts[f.rule] += 1
                d["rules"][f.rule] += 1
            if rules:
                d["flagged"] += 1
                if len(flagged) < flagged_limit:
                    flagged.append({"id": s.id, "date": s.study_datetime.isoformat(
                        timespec="minutes") if s.study_datetime else None,
                        "modality": s.modality, "device_name": names.get(dev),
                        "description": s.study_description,
                        "findings": [f.as_dict() for f in found if f.rule in rules]})
        session.expunge_all()
    devices = []
    for d in per_device.values():
        n = d["studies"] or 1
        devices.append({**{k: v for k, v in d.items() if k != "rules"},
                        "rules": dict(d["rules"]),
                        "share": {k: round(d[k] / n, 4) for k in
                                  ("exam_code", "age", "weight", "weight_measured", "sex")}})
    devices.sort(key=lambda x: -x["studies"])
    return {"studies": len(ids), "rules": {k: {"severity": v[0], "title": v[1],
                                                "count": rule_counts.get(k, 0)}
                                            for k, v in RULES.items()},
            "devices": devices, "flagged": flagged}
