"""Alert evaluation: rules (alert_rule) applied to studies, one alert per rule and study.

Runs for each ingested study (best effort, never blocks an import) and in batch
(``dosetrail alerts evaluate``) for history. Re-evaluating is idempotent: an existing
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
                code = national.get(None)
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
            elif rule.kind in ("cumulative", "count") and s.patient_id and s.study_datetime:
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
        studies = list(session.scalars(select(Study).where(Study.id.in_(chunk))))
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


def listing(session: Session, status: str | None = "open", limit: int = 50,
            names: dict[int, str] | None = None) -> list[dict]:
    """Alerts for the UI, newest study first (shape of the overview alert queue)."""
    q = (select(Alert, AlertRule, Study).join(AlertRule, AlertRule.id == Alert.rule_id)
         .join(Study, Study.id == Alert.study_id))
    if status:
        q = q.where(Alert.status == status)
    q = q.order_by(Alert.study_datetime.desc(), Alert.id.desc()).limit(limit)
    names = names or {d.id: d.display_name or f"Device {d.id}"
                      for d in session.scalars(select(Device))}
    kinds = {"threshold": None, "drl_ratio": "> DRL", "cumulative": "CUMUL.", "count": "REPEAT"}
    out = []
    for a, r, s in session.execute(q):
        kind = kinds.get(r.kind) or {"kar_total": "KA,R", "dap_total": "DAP",
                                     "fluoro_time": "FLUORO", "ctdivol_max": "CTDI"}.get(
            r.metric or "", "DOSE")
        acc = None if s.accession_is_hashed else s.accession
        out.append({
            "id": a.id, "study_id": s.id, "level": a.severity, "kind": kind,
            "rule": r.key, "rule_name": r.name,
            "title": " · ".join(x for x in (s.study_description, names.get(s.device_id))
                                if x) or s.modality,
            "accession": acc or f"#{s.id}",
            "time": a.study_datetime.isoformat(timespec="minutes") if a.study_datetime else None,
            "value": a.message.split(" ≥ ")[0] if " ≥ " in a.message else a.message,
            "limit": (f"≥ {_fmt(a.limit_value, a.unit)}" if a.limit_value is not None
                      and r.kind != "drl_ratio" else r.name),
            "message": a.message, "status": a.status, "ack_by": a.ack_by,
            "ack_at": a.ack_at.isoformat(timespec="minutes") if a.ack_at else None,
            "ack_note": a.ack_note,
        })
    return out
