"""Alert evaluation: rules (alert_rule) applied to studies, one alert per rule and study.

Runs for each ingested study (best effort, never blocks an import) and in batch
(``dosimetrix alerts evaluate``) for history. Re-evaluating is idempotent: an existing
alert for the same rule and study is kept as it is, with its acknowledgement.

Notifications are separate (``notify.dispatch``), so a slow mail server never delays
ingestion.

Device rules (``device_silent``, ``device_drift``) look at a device over time, not at one
study: ``evaluate_devices`` runs them as of a moment (hourly from the receiver, and over
history from ``dosimetrix alerts evaluate``). Their alert hangs on the device's latest
study, and its ``study_datetime`` is the moment the condition was seen.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import hybrid
from .db.models import (
    Alert,
    AlertRule,
    Classification,
    Code,
    CodeSystem,
    Device,
    Event,
    Study,
)
from .stats import METRICS

STUDY_METRICS = {k: m.column for k, m in METRICS.items() if m.level == "study"}
# per-series / per-exposure metrics: a threshold rule takes the study's highest value
EVENT_VALUE = {"ssde": lambda e: e.ssde if e.ssde is not None else e.ssde_calc}
UNITS = {"Gy": "Gy", "Gy.cm2": "Gy·cm²", "mGy": "mGy", "mGy.cm": "mGy·cm", "s": "s"}
BATCH = 1000
DEVICE_KINDS = ("device_silent", "device_drift", "drl_sustained")
RETIRED_DAYS = 90  # a device silent for longer is retired, not broken (it alerted at day 4)
SUSTAINED_MIN_N = 20  # as the DRL page: fewer values per room and quarter are not compared
# device drift: the last two weeks, each against the 12 weeks before them
DRIFT_WEEK, DRIFT_BASE = dt.timedelta(days=7), dt.timedelta(days=84)
DRIFT_MIN_WEEK, DRIFT_MIN_BASE = 10, 30


def _num(v: float) -> str:
    """3 significant digits without exponents: 1130, 8.22, 0.92."""
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.3g}"


def _fmt(v: float, unit: str | None) -> str:
    if unit == "s" and v >= 60:
        return f"{v / 60:.1f} min"
    return f"{v:.3g} {UNITS.get(unit or '', unit or '')}".strip()


def _applies(rule: AlertRule, s: Study) -> bool:
    if rule.modality and not hybrid.matches(s.modality, s.total_dlp, _modalities(rule)):
        return False
    return not (rule.max_age_days is not None
                and (s.age_days is None or s.age_days >= rule.max_age_days))


def _metric_value(metric: str | None, s: Study, events: list[Event]) -> tuple:
    """(value, where): a study metric, or the highest value of a per-event metric over the
    study's events (localizers left out), with the series it came from."""
    m = METRICS.get(metric or "")
    if m is None:
        return None, ""
    if m.level == "study":
        return getattr(s, m.column.key), ""
    get = EVENT_VALUE.get(metric) or (lambda e: getattr(e, m.column.key))
    best = max(((v, e) for e in events if not e.is_localizer
                for v in (get(e),) if v is not None), default=None, key=lambda x: x[0])
    if best is None:
        return None, ""
    return best[0], f" ({best[1].protocol})" if best[1].protocol else ""


class Evaluator:
    def __init__(self, session: Session):
        from .quality import Checker

        self.session = session
        self.rules = list(session.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))))
        self.checker = Checker(session) if any(r.kind == "drl_ratio" for r in self.rules) \
            else None

    def _drl_ratio(self, s: Study, events: list[Event], national: dict) -> tuple | None:
        """The highest (value, reference, unit, code) ratio of the study's exams."""
        if self.checker is None:
            return None
        band = self.checker.band(s.age_days)
        best = None
        sums: dict[str, float] = defaultdict(float)
        for e in events:
            if e.is_localizer:
                continue
            if s.modality == "CT":
                code = national.get(e.id) or national.get(None)
                ref = self.checker.drl.get((code, band, "dlp"))
                if ref and e.dlp:
                    cand = (e.dlp / ref, e.dlp, ref, "mGy.cm", code)
                    best = max(best, cand) if best else cand
            elif e.dap:
                code = national.get(e.id) if s.modality in ("DX", "CR", "MG") \
                    else national.get(None)
                if code:
                    sums[code] += e.dap
        for code, dap in sums.items():
            ref = self.checker.drl.get((code, band, "dap"))
            if ref:
                cand = (dap / ref, dap, ref, "Gy.cm2", code)
                best = max(best, cand) if best else cand
        return best

    def _window(self, rule: AlertRule, s: Study) -> tuple[float, int]:
        """Sum of the metric and number of studies for the patient over the window."""
        lo = s.study_datetime - dt.timedelta(days=rule.window_days or 0)
        q = select(func.count(), func.sum(STUDY_METRICS.get(rule.metric, Study.n_events))) \
            .where(Study.patient_id == s.patient_id, Study.study_datetime > lo,
                   Study.study_datetime <= s.study_datetime)
        if rule.modality:
            q = q.where(hybrid.modality_in(_modalities(rule)))
        n, total = self.session.execute(q).one()
        return float(total or 0), int(n or 0)

    def _raised_in_window(self, rule: AlertRule, s: Study) -> bool:
        """Per-patient rules alert once per window: not again for every further study of
        the same patient (the next alert comes after a new window has passed)."""
        # either side of the study: studies may be evaluated out of date order (a backfill
        # runs newest first)
        w = dt.timedelta(days=rule.window_days or 0)
        return self.session.scalar(
            select(func.count()).select_from(Alert).join(Study, Study.id == Alert.study_id)
            .where(Alert.rule_id == rule.id, Study.patient_id == s.patient_id,
                   Alert.study_id != s.id,
                   Alert.study_datetime > s.study_datetime - w,
                   Alert.study_datetime < s.study_datetime + w)) > 0

    def check(self, s: Study, events: list[Event], national: dict) -> list[dict]:
        out = []
        for rule in self.rules:
            if not _applies(rule, s):
                continue
            hit = None
            if rule.kind == "threshold":
                v, where = _metric_value(rule.metric, s, events)
                if v is not None and v >= rule.threshold:
                    hit = (v, rule.threshold, rule.unit,
                           f"{METRICS[rule.metric].label} {_fmt(v, rule.unit)} ≥ "
                           f"{_fmt(rule.threshold, rule.unit)}{where}")
            elif rule.kind == "drl_ratio":
                r = self._drl_ratio(s, events, national)
                if r and r[0] >= rule.threshold:
                    ratio, v, ref, unit, code = r
                    hit = (v, ref * rule.threshold, unit,
                           f"{code}: {_fmt(v, unit)} = {ratio:.1f} × DRL {_fmt(ref, unit)}")
            elif rule.kind == "pregnant":
                if s.pregnant:
                    weeks = f", {s.gestation_weeks} weeks" if s.gestation_weeks else ""
                    hit = (float(s.gestation_weeks or 0), None, None,
                           f"Pregnant patient{weeks}: {s.modality}")
            elif (rule.kind in ("cumulative", "count") and s.patient_id and s.study_datetime
                  and not self._raised_in_window(rule, s)):
                total, n = self._window(rule, s)
                if rule.kind == "cumulative" and total >= rule.threshold and n > 1:
                    hit = (total, rule.threshold, rule.unit,
                           f"Σ {_fmt(total, rule.unit)} in {n} studies / "
                           f"{rule.window_days} d ≥ {_fmt(rule.threshold, rule.unit)}")
                elif rule.kind == "count" and n >= rule.threshold:
                    hit = (float(n), rule.threshold, None,
                           f"{n} studies in {rule.window_days} days")
            if hit:
                v, limit, unit, msg = hit
                out.append({"rule_id": rule.id, "study_id": s.id,
                            "study_datetime": s.study_datetime, "severity": rule.severity,
                            "value": v, "limit_value": limit, "unit": unit,
                            "message": msg[:512], "status": "open"})
        return out


def evaluate(session: Session, study_ids: Iterable[int],
             evaluator: Evaluator | None = None) -> Counter:
    """Evaluate the enabled rules for the studies; insert new alerts. Returns counters."""
    from .quality import _national_codes

    ev = evaluator or Evaluator(session)
    stats: Counter = Counter()
    if not ev.rules:
        return stats
    ids = sorted(set(study_ids))
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = list(session.scalars(select(Study).where(Study.id.in_(chunk))
                                       .order_by(Study.study_datetime, Study.id)))
        events: dict[int, list[Event]] = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_(chunk))):
            events[e.study_id].append(e)
        national = _national_codes(session, chunk)
        existing = {(a.rule_id, a.study_id): a for a in session.scalars(
            select(Alert).where(Alert.study_id.in_(chunk)))}
        for s in studies:
            stats["studies"] += 1
            for a in ev.check(s, events.get(s.id, []), national.get(s.id, {})):
                old = existing.get((a["rule_id"], a["study_id"]))
                if old is not None:
                    # a cumulative dose report may raise the value: keep the alert (and
                    # its acknowledgement) but show the current value while it is open
                    if old.status == "open" and old.value != a["value"]:
                        old.value, old.message = a["value"], a["message"]
                        stats["updated"] += 1
                    else:
                        stats["kept"] += 1
                    continue
                try:
                    with session.begin_nested():
                        session.add(Alert(**a))
                except IntegrityError:  # a concurrent worker raised it first
                    stats["kept"] += 1
                    continue
                stats["new"] += 1
                stats[f"new_{a['severity']}"] += 1
        session.flush()
    return stats


def evaluate_range(session: Session, start: dt.datetime | None = None,
                   end: dt.datetime | None = None, progress=None) -> Counter:
    q = select(Study.id)
    if start:
        q = q.where(Study.study_datetime >= start)
    if end:
        q = q.where(Study.study_datetime < end)
    ids = list(session.scalars(q.order_by(Study.study_datetime)))
    ev = Evaluator(session)
    total: Counter = Counter()
    for i in range(0, len(ids), 20 * BATCH):
        total += evaluate(session, ids[i:i + 20 * BATCH], ev)
        session.commit()
        if progress:
            progress(f"{min(i + 20 * BATCH, len(ids))}/{len(ids)} studies, "
                     f"{total['new']} new alerts")
    return total


def counts(session: Session, status: str | None = None, **filters) -> dict:
    """Total and per rule / level / modality for the filtered alerts."""
    base = (select(AlertRule.key, AlertRule.name, Alert.severity, Study.modality, func.count())
            .select_from(Alert).join(AlertRule, AlertRule.id == Alert.rule_id)
            .join(Study, Study.id == Alert.study_id))
    rows = session.execute(_filtered(base, status, **filters)
                           .group_by(AlertRule.key, AlertRule.name, Alert.severity,
                                     Study.modality)).all()
    by_rule: dict[str, dict] = {}
    by_level: Counter = Counter()
    by_mod: Counter = Counter()
    for _key, _name, sev, mod, n in rows:
        by_level[sev] += n
        by_mod[mod] += n
    # the per-rule counts ignore the rule filter, so the rule dropdown keeps listing every rule
    for key, name, n in session.execute(
            _filtered(select(AlertRule.key, AlertRule.name, func.count())
                      .select_from(Alert).join(AlertRule, AlertRule.id == Alert.rule_id)
                      .join(Study, Study.id == Alert.study_id),
                      status, **{**filters, "rule": None})
            .group_by(AlertRule.key, AlertRule.name)):
        by_rule[key] = {"rule": key, "name": name, "n": n}
    return {"total": sum(by_level.values()), "by_rule": sorted(by_rule.values(),
                                                                key=lambda x: -x["n"]),
            "by_level": dict(by_level), "by_modality": dict(by_mod)}


def acknowledge(session: Session, ids: list[int], username: str, note: str | None,
                when: dt.datetime) -> int:
    n = 0
    for a in session.scalars(select(Alert).where(Alert.id.in_(ids), Alert.status == "open")):
        a.status, a.ack_by, a.ack_at = "acknowledged", username, when
        a.ack_note = (note or "")[:1024] or None
        n += 1
    session.flush()
    return n


def open_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Alert)
                          .where(Alert.status == "open")) or 0


def _value_limit(a: Alert, r: AlertRule) -> tuple[str, str]:
    """Short value and limit texts for the alert queue."""
    v = a.value or 0.0
    if r.kind == "drl_ratio" and a.limit_value:
        ref = a.limit_value / r.threshold
        return _fmt(v, a.unit), f"DRL {_fmt(ref, a.unit)} ({v / ref:.1f}×)"
    if r.kind == "cumulative":
        return f"Σ {_fmt(v, a.unit)}", f"≥ {_fmt(r.threshold, a.unit)} / {r.window_days} d"
    if r.kind == "count":
        return f"{v:.0f} studies", f"≥ {r.threshold:.0f} / {r.window_days} d"
    if r.kind == "device_silent":
        return f"{v:.1f} days", f"≥ {r.threshold:g} d"
    if r.kind == "drl_sustained":
        return f"{v:.2f}× DRL", f"≥ {r.threshold:g}× (2 quarters)"
    if r.kind == "device_drift":
        return f"{v:.2f}×", f"≥ {r.threshold:g}× (2 weeks)"
    if r.kind == "pregnant":
        return (f"{v:.0f} weeks" if v else "pregnant"), "—"
    return _fmt(v, a.unit), f"≥ {_fmt(a.limit_value or r.threshold, a.unit)}"


def _filtered(q, status=None, rule=None, level=None, since=None, until=None, modality=None):
    if status:
        q = q.where(Alert.status == status)
    if rule:
        q = q.where(AlertRule.key == rule)
    if level:
        q = q.where(Alert.severity == level)
    if since:
        q = q.where(Alert.study_datetime >= since)
    if until:
        q = q.where(Alert.study_datetime < until)
    if modality:
        q = q.where(Study.modality == modality)
    return q


def listing(session: Session, status: str | None = "open", limit: int = 50,
            names: dict[int, str] | None = None, offset: int = 0, **filters) -> list[dict]:
    """Alerts for the UI, newest study first (shape of the overview alert queue).
    Filters: rule (key), level, since/until (datetime), modality."""
    q = (select(Alert, AlertRule, Study).join(AlertRule, AlertRule.id == Alert.rule_id)
         .join(Study, Study.id == Alert.study_id))
    q = _filtered(q, status, **filters)
    q = q.order_by(Alert.study_datetime.desc(), Alert.id.desc()).offset(offset).limit(limit)
    names = names or {d.id: d.display_name or f"Device {d.id}"
                      for d in session.scalars(select(Device))}
    kinds = {"threshold": None, "drl_ratio": "> DRL", "cumulative": "CUMUL.", "count": "REPEAT",
             "pregnant": "PREGNANT", "device_silent": "SILENT", "device_drift": "DRIFT",
             "drl_sustained": "> DRL ×2Q"}
    out = []
    for a, r, s in session.execute(q):
        kind = kinds.get(r.kind) or {"kar_total": "KA,R", "dap_total": "DAP",
                                     "fluoro_time": "FLUORO", "ctdivol_max": "CTDI",
                                     "dlp_total": "DLP", "psd": "PSD", "ssde": "SSDE"}.get(
            r.metric or "", "DOSE")
        acc = None if s.accession_is_hashed else s.accession
        value, limit = _value_limit(a, r)
        out.append({
            "id": a.id, "study_id": s.id, "level": a.severity, "kind": kind,
            "rule": r.key, "rule_name": r.name,
            "title": " · ".join(x for x in (
                a.message.split(":")[0] if r.kind == "drl_ratio" else None,
                None if r.kind in DEVICE_KINDS else s.study_description or s.modality,
                names.get(s.device_id)) if x),
            "accession": acc or f"#{s.id}",
            "time": a.study_datetime.isoformat(timespec="minutes") if a.study_datetime else None,
            "value": value, "limit": limit,
            "message": a.message, "status": a.status, "ack_by": a.ack_by,
            "ack_at": a.ack_at.isoformat(timespec="minutes") if a.ack_at else None,
            "ack_note": a.ack_note, "modality": s.modality, "severity": a.severity,
        })
    return out


def window_studies(session: Session, alert_id: int,
                   names: dict[int, str] | None = None) -> list[dict] | None:
    """The patient's studies behind a per-patient (cumulative / count) alert: every study in
    the rule's window ending at the alert's study, the study itself included (``current``).
    None if the alert does not exist; an empty list for rules that are not per-patient."""
    a = session.get(Alert, alert_id)
    if a is None:
        return None
    r, s = session.get(AlertRule, a.rule_id), session.get(Study, a.study_id)
    if r.kind not in ("cumulative", "count") or not s.patient_id or not s.study_datetime:
        return []
    names = names or {d.id: d.display_name or f"Device {d.id}"
                      for d in session.scalars(select(Device))}
    lo = s.study_datetime - dt.timedelta(days=r.window_days or 0)
    q = select(Study).where(Study.patient_id == s.patient_id, Study.study_datetime > lo,
                            Study.study_datetime <= s.study_datetime)
    if r.modality:
        q = q.where(hybrid.modality_in(_modalities(r)))
    return [{"id": x.id, "current": x.id == s.id,
             "accession": None if x.accession_is_hashed else x.accession,
             "time": x.study_datetime.isoformat(timespec="minutes"), "modality": x.modality,
             "description": x.study_description, "device": names.get(x.device_id),
             "dap": x.total_dap, "dlp": x.total_dlp, "ctdivol_max": x.ctdivol_max,
             "kar": x.total_rp_dose}
            for x in session.scalars(q.order_by(Study.study_datetime, Study.id))]


# ------------------------------------------------------------------ device rules


def _local_devices(session: Session) -> dict[int, str]:
    return {d.id: d.display_name or d.station_name or f"Device {d.id}"
            for d in session.scalars(select(Device).where(Device.is_local.is_(True),
                                                          Device.merged_into_id.is_(None)))}


def _modalities(rule: AlertRule) -> list[str] | None:
    return [m.strip().upper() for m in rule.modality.split(",")] if rule.modality else None


def _latest_study(session: Session, device_id: int, lo: dt.datetime, hi: dt.datetime,
                  mods: list[str] | None) -> int | None:
    q = select(Study.id).where(Study.device_id == device_id, Study.study_datetime > lo,
                               Study.study_datetime <= hi)
    if mods:
        q = q.where(Study.modality.in_(mods))
    return session.scalar(q.order_by(Study.study_datetime.desc(), Study.id.desc()).limit(1))


def _silent(session: Session, rule: AlertRule, now: dt.datetime) -> list[dict]:
    """Local devices with no study for ``threshold`` days that, over the ``window_days``
    (default 28) before their last study, averaged at least one study a day: a broken
    dose report export or PACS link rather than a quiet room. Not after 90 days (retired)."""
    window = dt.timedelta(days=rule.window_days or 28)
    mods = _modalities(rule)
    devices = _local_devices(session)
    q = (select(Study.device_id, func.max(Study.study_datetime))
         .where(Study.device_id.in_(devices), Study.study_datetime <= now)
         .group_by(Study.device_id))
    if mods:
        q = q.where(Study.modality.in_(mods))
    out = []
    for did, last in session.execute(q):
        days = (now - last).total_seconds() / 86400
        if not rule.threshold <= days <= RETIRED_DAYS:  # longer: a retired room
            continue
        cq = select(func.count()).where(Study.device_id == did,
                                        Study.study_datetime > last - window,
                                        Study.study_datetime <= last)
        if mods:
            cq = cq.where(Study.modality.in_(mods))
        n = session.scalar(cq)
        if n < window.days:
            continue
        out.append({"study_id": _latest_study(session, did, last - dt.timedelta(seconds=1),
                                              last, mods),
                    "value": days, "limit_value": rule.threshold, "unit": "d",
                    "message": f"{devices[did]}: no studies for {days:.1f} days (last "
                               f"{last:%Y-%m-%d %H:%M}; {n} in the {window.days} days "
                               "before)"})
    return out


def _drift_samples(session: Session, lo: dt.datetime, hi: dt.datetime,
                   devices: list[int], mods: list[str] | None) -> list[tuple]:
    """(device, national exam code, age days, time, dose, study id): CT DLP and
    fluoroscopy DAP per study, radiography DAP per projection."""
    sys_id = session.scalar(select(CodeSystem.id).where(CodeSystem.key == "ro-drl-ms838-2025"))
    if sys_id is None:
        return []
    base = [Study.device_id.in_(devices), Study.study_datetime > lo, Study.study_datetime <= hi]
    out = []
    study_mods = [m for m in ("CT", "RF", "XA") if not mods or m in mods]
    if study_mods:
        dose = func.coalesce(Study.total_dlp, Study.total_dap)
        out += session.execute(
            select(Study.device_id, Code.code, Study.age_days, Study.study_datetime, dose,
                   Study.id)
            .join(Classification, (Classification.study_id == Study.id)
                  & Classification.event_id.is_(None))
            .join(Code, (Code.id == Classification.code_id) & (Code.system_id == sys_id))
            .where(*base, Study.modality.in_(study_mods), dose > 0)).all()
    rx_mods = [m for m in ("DX", "CR") if not mods or m in mods]
    if rx_mods:
        out += session.execute(
            select(Study.device_id, Code.code, Study.age_days, Study.study_datetime, Event.dap,
                   Study.id)
            .join(Event, Event.study_id == Study.id)
            .join(Classification, Classification.event_id == Event.id)
            .join(Code, (Code.id == Classification.code_id) & (Code.system_id == sys_id))
            .where(*base, Study.modality.in_(rx_mods), Event.dap > 0)).all()
    return out


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _drift(session: Session, rule: AlertRule, now: dt.datetime) -> list[dict]:
    """A device's median dose for an exam and age group, in each of the last two weeks, at
    least ``threshold`` × its median over the 12 weeks before (≥ 10 values a week, ≥ 30
    before). Age groups and two weeks running keep case mix from raising it. Not raised
    again for the same device, exam and group within ``window_days`` (default 28)."""
    from .stats import BandLookup

    devices = _local_devices(session)
    if not devices:
        return []
    band = BandLookup(session, "ro-drl-age", "age")
    w1, w2, w0 = now - DRIFT_WEEK, now - 2 * DRIFT_WEEK, now - 2 * DRIFT_WEEK - DRIFT_BASE
    groups: dict[tuple, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for did, code, age, t, v, sid in _drift_samples(session, w0, now, list(devices),
                                                    _modalities(rule)):
        b = band(age)
        if b is None:
            continue
        part = "w1" if t > w1 else "w2" if t > w2 else "base"
        groups[(did, code, b)][part].append((t, v, sid))
    quiet = dt.timedelta(days=rule.window_days or 28)
    out = []
    for (did, code, b), g in sorted(groups.items()):
        if len(g["base"]) < DRIFT_MIN_BASE or min(len(g["w1"]), len(g["w2"])) < DRIFT_MIN_WEEK:
            continue
        ref = _median([v for _, v, _ in g["base"]])
        r1, r2 = (_median([v for _, v, _ in g[w]]) / ref for w in ("w1", "w2"))
        if min(r1, r2) < rule.threshold:
            continue
        key = f"{code} {band.label.get(b, b)} · {devices[did]}"
        if session.scalar(
                select(func.count()).select_from(Alert).join(Study, Study.id == Alert.study_id)
                .where(Alert.rule_id == rule.id, Study.device_id == did,
                       Alert.study_datetime > now - quiet, Alert.study_datetime <= now,
                       Alert.message.startswith(key + ":"))):
            continue
        unit = "mGy·cm" if code.startswith("CT") else "Gy·cm²"
        out.append({"study_id": max(g["w1"])[2], "value": r1, "limit_value": rule.threshold,
                    "unit": None,
                    "message": f"{key}: median {_num(_median([v for _, v, _ in g['w1']]))} "
                               f"{unit} this week ({len(g['w1'])}), {r2:.2f}× the week "
                               f"before; 12 weeks before {_num(ref)} {unit} "
                               f"({len(g['base'])})"})
    return out


def _quarter_start(t: dt.datetime) -> dt.datetime:
    return dt.datetime(t.year, 3 * ((t.month - 1) // 3) + 1, 1)


def _prev_quarter(q: dt.datetime) -> dt.datetime:
    return dt.datetime(q.year - (q.month == 1), (q.month - 4) % 12 + 1, 1)


def _sustained(session: Session, rule: AlertRule, now: dt.datetime) -> list[dict]:
    """Rooms whose median for an exam and age group was ``>= threshold`` × the DRL in each
    of the last two complete quarters (≥ 20 values in each), as the DRL page computes it:
    the trigger for an optimisation review. The reference set is ``metric`` (default the
    national one; a local set's key works too). Once per room, exam, group and quarter."""
    from . import drl as drl_mod
    from .api.data import _physical_ids, device_names, overrides
    from .api.query import conditions, parse

    set_key = rule.metric or "ro-ms838-2025"
    q2 = _prev_quarter(_quarter_start(now))
    q1 = _prev_quarter(q2)
    base = conditions(parse("origin:local"), now, overrides(session))
    physical, names = _physical_ids(session), device_names(session)
    found = []
    for lo, hi in ((q1, q2), (q2, _quarter_start(now))):
        try:
            res = drl_mod.compare(session, [*base, Study.study_datetime >= lo,
                                            Study.study_datetime < hi], set_key,
                                  by_device=True, min_n=SUSTAINED_MIN_N, physical=physical,
                                  names=names)
        except drl_mod.DRLError:
            return []
        found.append({(r["exam"], r["band_label"], r["quantity"], g["device"]): (r, g)
                      for r in res["rows"] for g in r.get("groups", ())
                      if g["device"] and g["n"] >= SUSTAINED_MIN_N and g["ratio"]
                      and g["ratio"] >= rule.threshold})
    label = f"{q2.year}-Q{(q2.month - 1) // 3 + 1}"
    out = []
    for k in sorted(set(found[0]) & set(found[1]), key=str):
        (r, g), (_, g0) = found[1][k], found[0][k]
        exam, band, quantity, did = k
        key = f"{label} {exam} {band or 'all ages'} {quantity} · {g['device_name']}"
        if session.scalar(select(func.count()).select_from(Alert)
                          .where(Alert.rule_id == rule.id, Alert.message.startswith(key + ":"))):
            continue
        unit = UNITS.get(r["unit"], r["unit"])
        out.append({"study_id": _latest_study(session, did, q2, _quarter_start(now), None),
                    "value": g["ratio"], "limit_value": rule.threshold, "unit": None,
                    "message": f"{key}: median {_num(g['median'])} {unit} = {g['ratio']:.2f}× "
                               f"DRL {_num(r['reference'])} ({g['n']}); quarter before "
                               f"{g0['ratio']:.2f}× ({g0['n']})"})
    return out


DEVICE_CHECKS = {"device_silent": _silent, "device_drift": _drift, "drl_sustained": _sustained}


def evaluate_devices(session: Session, now: dt.datetime | None = None,
                     kinds: Iterable[str] = DEVICE_KINDS) -> Counter:
    """Run the enabled device rules as of ``now``; insert new alerts, refresh open ones."""
    now = now or dt.datetime.now()
    stats: Counter = Counter()
    rules = list(session.scalars(select(AlertRule).where(AlertRule.enabled.is_(True),
                                                         AlertRule.kind.in_(list(kinds)))))
    for rule in rules:
        for h in DEVICE_CHECKS[rule.kind](session, rule, now):
            if h["study_id"] is None:
                continue
            old = session.scalar(select(Alert).filter_by(rule_id=rule.id,
                                                         study_id=h["study_id"]))
            if old is not None:
                if old.status == "open" and old.value != h["value"]:
                    old.value, old.message = h["value"], h["message"][:512]
                    stats["updated"] += 1
                else:
                    stats["kept"] += 1
                continue
            try:
                with session.begin_nested():
                    session.add(Alert(rule_id=rule.id, study_datetime=now,
                                      severity=rule.severity, status="open",
                                      **{**h, "message": h["message"][:512]}))
            except IntegrityError:
                stats["kept"] += 1
                continue
            stats["new"] += 1
            stats[f"new_{rule.severity}"] += 1
    session.flush()
    return stats


def evaluate_devices_range(session: Session, start: dt.datetime, end: dt.datetime | None = None,
                           progress=None) -> Counter:
    """Device rules over history: silence day by day, drift week by week (and at the end)."""
    end = end or dt.datetime.now()
    steps = [start + dt.timedelta(days=d) for d in range(max((end - start).days, 0) + 1)]
    if not steps or steps[-1] < end:
        steps.append(end)
    total: Counter = Counter()
    for i, t in enumerate(steps):
        last = i == len(steps) - 1
        total += evaluate_devices(session, t, DEVICE_KINDS if i % 7 == 0 or last
                                  else ("device_silent",))
        session.commit()
        if progress and (i % 30 == 0 or last):
            progress(f"devices as of {t:%Y-%m-%d}: {total['new']} new alerts")
    return total
