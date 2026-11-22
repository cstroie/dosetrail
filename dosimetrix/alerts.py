"""Alert evaluation: rules (alert_rule) applied to studies, one alert per rule and study.

Runs for each ingested study (best effort, never blocks an import) and in batch
(``dosimetrix alerts evaluate``) for history. Re-evaluating is idempotent: an existing
alert for the same rule and study is kept as it is, with its acknowledgement.

Notifications are separate (``notify.dispatch``), so a slow mail server never delays
ingestion.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db.models import Alert, AlertRule, Device, Event, Study
from .stats import METRICS

STUDY_METRICS = {k: m.column for k, m in METRICS.items() if m.level == "study"}
UNITS = {"Gy": "Gy", "Gy.cm2": "Gy·cm²", "mGy": "mGy", "mGy.cm": "mGy·cm", "s": "s"}
BATCH = 1000


def _fmt(v: float, unit: str | None) -> str:
    if unit == "s" and v >= 60:
        return f"{v / 60:.1f} min"
    return f"{v:.3g} {UNITS.get(unit or '', unit or '')}".strip()


def _applies(rule: AlertRule, s: Study) -> bool:
    if rule.modality and s.modality not in {m.strip().upper()
                                             for m in rule.modality.split(",")}:
        return False
    return not (rule.max_age_days is not None
                and (s.age_days is None or s.age_days >= rule.max_age_days))


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
            q = q.where(Study.modality.in_([m.strip().upper()
                                            for m in rule.modality.split(",")]))
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
                col = STUDY_METRICS.get(rule.metric)
                v = getattr(s, col.key) if col is not None else None
                if v is not None and v >= rule.threshold:
                    hit = (v, rule.threshold, rule.unit,
                           f"{METRICS[rule.metric].label} {_fmt(v, rule.unit)} ≥ "
                           f"{_fmt(rule.threshold, rule.unit)}")
            elif rule.kind == "drl_ratio":
                r = self._drl_ratio(s, events, national)
                if r and r[0] >= rule.threshold:
                    ratio, v, ref, unit, code = r
                    hit = (v, ref * rule.threshold, unit,
                           f"{code}: {_fmt(v, unit)} = {ratio:.1f} × DRL {_fmt(ref, unit)}")
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
    for key, name, sev, mod, n in rows:
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
    kinds = {"threshold": None, "drl_ratio": "> DRL", "cumulative": "CUMUL.", "count": "REPEAT"}
    out = []
    for a, r, s in session.execute(q):
        kind = kinds.get(r.kind) or {"kar_total": "KA,R", "dap_total": "DAP",
                                     "fluoro_time": "FLUORO", "ctdivol_max": "CTDI"}.get(
            r.metric or "", "DOSE")
        acc = None if s.accession_is_hashed else s.accession
        value, limit = _value_limit(a, r)
        out.append({
            "id": a.id, "study_id": s.id, "level": a.severity, "kind": kind,
            "rule": r.key, "rule_name": r.name,
            "title": " · ".join(x for x in (
                a.message.split(":")[0] if r.kind == "drl_ratio" else None,
                s.study_description or s.modality, names.get(s.device_id)) if x),
            "accession": acc or f"#{s.id}",
            "time": a.study_datetime.isoformat(timespec="minutes") if a.study_datetime else None,
            "value": value, "limit": limit,
            "message": a.message, "status": a.status, "ack_by": a.ack_by,
            "ack_at": a.ack_at.isoformat(timespec="minutes") if a.ack_at else None,
            "ack_note": a.ack_note, "modality": s.modality, "severity": a.severity,
        })
    return out
