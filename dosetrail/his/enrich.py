"""Study enrichment from the HIS: patient, weight, height, the imaging request.

Flow per study (queued in ``his_lookup`` when the study is first stored):
1. find the patient by the study's patient ID (a national ID, or a HIS patient code);
   reject a HIS record whose birth date is far from the header's (another person's ID);
2. sex and birth date: CNP > HIS > DICOM (demographics.reconcile), exact ages recomputed;
3. device policy ``full``: the patient's requests of the matching type whose order time is
   in [exam − window_before_h, exam + window_after_h]; the closest one before the exam wins
   (body region as tie-break); its order form gives weight, height, pregnancy, ward,
   requester, indication, guideline decision → ``study_request``;
4. weight / height by rank: measured > request same day > request ≤ 72 h > DICOM header /
   legacy import > HIS chart (undated) > estimates. A value replaces only a lower rank, and
   only when plausible for the age and sex (growth tables for children, wide bounds otherwise);
5. the derived values (weight estimates, DRL exclusions, skin dose, alerts) are recomputed.

Retries: a new study whose request isn't found (forms are often completed after the exam)
or a HIS that is busy is tried again after 15 min, 2 h and 24 h. History (backfill) studies
get a second try a day later, and run only within the server's backfill hours.
The gateway's login is checked before every batch and after a run of "not found" answers:
a broken HIS session makes every search look empty.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import demographics
from ..db.models import (
    Device,
    HisLookup,
    HisServer,
    Patient,
    SizeSource,
    Study,
    StudyRequest,
)
from . import servers
from .hippobridge import HisError, HisPatient, HisRequest, HisRequestStub

log = logging.getLogger(__name__)

RETRY_AFTER = (dt.timedelta(minutes=15), dt.timedelta(hours=2), dt.timedelta(hours=24))
# History studies get one more try a day later: "not found" can also be a passing HIS fault.
RECENT = dt.timedelta(days=3)  # newer studies are retried and never wait for backfill hours
NOT_FOUND_RECHECK = 5  # consecutive "patient not found" answers before re-verifying the login

RANK = {None: 0, SizeSource.estimated_reference: 1, SizeSource.estimated_visual: 1,
        SizeSource.his_chart_undated: 2, SizeSource.dicom_unknown: 3, SizeSource.imported: 3,
        SizeSource.chart_72h: 4, SizeSource.chart_same_day: 5, SizeSource.measured: 6}


def kinds_for(modality: str | None) -> list[str]:
    """HIS request types to look in, in order."""
    m = (modality or "").upper()
    if m == "CT":
        return ["ct"]
    if m in ("RF", "XA"):
        return ["radio", "rads"]
    return ["radio"]


# ---------------------------------------------------------------------------- plausibility


def plausible_weight(kg: float | None, age_days: int | None, sex: str | None) -> str | None:
    """None when plausible, else the reason."""
    if not kg or kg <= 0:
        return "no value"
    if age_days is not None and age_days < 18 * 365.25:
        from .. import growth

        g = growth.tables()
        a = g.assess(sex, age_days, kg) if g else None
        if a is not None:  # P1–P99 outliers are real children too: only |z| > 5 is refused
            return f"unrealistic for age (z {a['z']})" if a["unrealistic"] else None
    if age_days is None:
        lo, hi = 0.3, 250.0
    else:
        from ..quality import weight_bounds

        lo, hi = weight_bounds(age_days)
    return None if lo <= kg <= hi else f"outside {lo:g}–{hi:g} kg for age"


_HEIGHT_BOUNDS = ((1, 0.30, 0.95), (5, 0.45, 1.35), (10, 0.75, 1.70), (15, 0.95, 2.00))


def plausible_height(m: float | None, age_days: int | None, weight_kg: float | None
                     ) -> str | None:
    if not m or m <= 0:
        return "no value"
    lo, hi = 0.30, 2.30
    if age_days is not None:
        y = age_days / 365.25
        lo, hi = next(((a, b) for top, a, b in _HEIGHT_BOUNDS if y < top), (1.20, 2.30))
    if not lo <= m <= hi:
        return f"outside {lo:g}–{hi:g} m for age"
    if weight_kg:
        bmi = weight_kg / (m * m)
        if not 8 <= bmi <= 70:
            return f"BMI {bmi:.0f} with the weight"
    return None


# ------------------------------------------------------------------------------ matching


def choose_request(stubs: list[HisRequestStub], when: dt.datetime, before_h: int, after_h: int,
                   region_hint: str | None = None) -> tuple[HisRequestStub | None, float | None]:
    """The request ordered closest before the exam (or just after), with a score 0–1."""
    lo, hi = when - dt.timedelta(hours=before_h), when + dt.timedelta(hours=after_h)
    cands = [s for s in stubs if s.authored_on and lo <= s.authored_on <= hi]
    if not cands:
        return None, None

    def key(s):
        delta = (when - s.authored_on).total_seconds()
        late = delta < 0  # ordered after the exam: less likely
        region_ok = bool(region_hint and s.region and region_hint.lower() in s.region.lower())
        return (late, not region_ok, abs(delta))

    cands.sort(key=key)
    best = cands[0]
    hours = abs((when - best.authored_on).total_seconds()) / 3600
    score = max(0.0, 1.0 - hours / max(before_h, 1))
    if len(cands) > 1 and abs(key(cands[1])[2] - key(best)[2]) < 3600 \
            and key(cands[1])[:2] == key(best)[:2]:
        score /= 2  # two candidates within the hour: ambiguous
    return best, round(score, 2)


# ------------------------------------------------------------------------------ writing


def _size_source(authored: dt.datetime | None, when: dt.datetime | None) -> SizeSource:
    if authored is None or when is None:
        return SizeSource.his_chart_undated
    if authored.date() == when.date():
        return SizeSource.chart_same_day
    if abs((when - authored).total_seconds()) <= 72 * 3600:
        return SizeSource.chart_72h
    return SizeSource.his_chart_undated


def apply_sizes(study: Study, sex: str | None, offers: list[tuple[str, float | None,
                float | None, SizeSource]], detail: dict) -> list[str]:
    """offers: (origin, weight_kg, height_m, source), best first. Returns what was written."""
    written = []
    for origin, w, _h, src in offers:
        if w is None:
            continue
        why = plausible_weight(w, study.age_days, sex)
        if why:
            detail.setdefault("rejected", []).append({"from": origin, "weight_kg": w, "why": why})
            continue
        if RANK[src] > RANK.get(study.weight_source, 0):
            if study.weight_source in (SizeSource.dicom_unknown, SizeSource.imported):
                study.weight_reported_kg = study.weight_kg
            study.weight_kg, study.weight_source = w, src
            written.append(f"weight {origin}")
        break  # the best plausible offer decides, even if it doesn't outrank the stored one
    for origin, _w, hgt, src in offers:
        if hgt is None:
            continue
        why = plausible_height(hgt, study.age_days, study.weight_kg)
        if why:
            detail.setdefault("rejected", []).append({"from": origin, "height_m": hgt, "why": why})
            continue
        if RANK[src] > RANK.get(study.height_source, 0):
            study.height_m, study.height_source = hgt, src
            written.append(f"height {origin}")
        break
    return written


def _save_request(session: Session, study: Study, server: HisServer, r: HisRequest) -> None:
    row = session.get(StudyRequest, study.id) or StudyRequest(study_id=study.id)
    row.his_server_id, row.request_id, row.authored_on = server.id, r.id[:32], r.authored_on
    for f in ("ward", "requester", "priority", "procedure", "region", "indication",
              "guideline", "prior_imaging", "pregnant", "weight_kg", "height_m"):
        v = getattr(r, f)
        limit = {"ward": 128, "requester": 128, "priority": 16, "procedure": 255,
                 "region": 64}.get(f, 1024)
        setattr(row, f, v[:limit] if isinstance(v, str) else v)
    session.add(row)


# ------------------------------------------------------------------------------ one study


@dataclass
class Context:
    """Per-run caches so one patient's studies cost one patient lookup and one listing."""

    patients: dict = field(default_factory=dict)
    listings: dict = field(default_factory=dict)
    not_found_streak: int = 0
    retry: bool = False  # the last study may gain data later (request form, patient record)


def process(session: Session, lookup: HisLookup, server: HisServer, client, ctx: Context,
            now: dt.datetime) -> str:
    """Look one study up; sets and returns the lookup status. Raises HisError."""
    study = session.get(Study, lookup.study_id)
    lookup.his_server_id, lookup.attempts = server.id, (lookup.attempts or 0) + 1
    lookup.fetched_at = now
    detail: dict = {}
    ctx.retry = False
    if study is None:
        lookup.status = "skipped"
        return lookup.status
    device = session.get(Device, study.device_id) if study.device_id else None
    policy = device.his_policy if device is not None else "full"
    modality = (device.modality_override if device is not None and device.modality_override
                else study.modality)
    pat = session.get(Patient, study.patient_id) if study.patient_id else None
    if policy == "none" or not servers.covers(server, modality) or pat is None \
            or not pat.patient_id or pat.is_test:
        lookup.status, lookup.detail = "skipped", {"why": "policy" if policy == "none" else
                                                   "modality" if pat is not None else
                                                   "no patient ID"}
        return lookup.status

    if pat.patient_id not in ctx.patients:
        ctx.patients[pat.patient_id] = client.find_patient(pat.patient_id)
    hp: HisPatient | None = ctx.patients[pat.patient_id]
    if hp is not None and demographics.another_person(pat, hp.birth_date):
        detail["rejected_patient"] = "birth date far from the header's: another person's ID?"
        hp = None
    if hp is None:
        ctx.not_found_streak += 1
        ctx.retry = "rejected_patient" not in detail
        lookup.status, lookup.detail = "no_patient", detail or None
        return lookup.status
    ctx.not_found_streak = 0
    lookup.his_patient_id = hp.id[:32]
    if demographics.reconcile(pat, his_sex=hp.sex, his_birth=hp.birth_date):
        demographics.refresh_ages(session, [pat.id])
    if pat.demo_conflict:
        detail["demographics"] = pat.demo_conflict

    req: HisRequest | None = None
    if policy == "full" and study.study_datetime is not None:
        for kind in kinds_for(modality):
            key = (hp.id, kind)
            if key not in ctx.listings:
                ctx.listings[key] = client.list_requests(hp.id, kind)
            stub, score = choose_request(ctx.listings[key], study.study_datetime,
                                         server.window_before_h, server.window_after_h)
            if stub is not None:
                req = client.get_request(stub.id)
                lookup.request_id, lookup.match_score = stub.id[:32], score
                detail["type"] = kind
                break
    if req is not None and req.national_id and req.national_id != pat.patient_id:
        detail["rejected_request"] = "the order form names another patient"
        req = None

    offers = []
    if req is not None:
        _save_request(session, study, server, req)
        if study.pregnant is None and req.pregnant is not None:
            study.pregnant = req.pregnant
        offers.append(("request", req.weight_kg, req.height_m,
                       _size_source(req.authored_on, study.study_datetime)))
    offers.append(("chart", hp.weight_kg, hp.height_m, SizeSource.his_chart_undated))
    written = apply_sizes(study, pat.sex, offers, detail)
    if written:
        detail["written"] = written
    lookup.status = "matched" if req is not None else "patient_only"
    ctx.retry = req is None and policy == "full"
    lookup.detail = detail or None
    session.flush()
    return lookup.status


def _retry(lookup: HisLookup, study_when: dt.datetime | None, now: dt.datetime,
           error: str | None = None) -> None:
    """Schedule another try for recent studies; history gets one try."""
    recent = study_when is not None and now - study_when <= RECENT
    schedule = RETRY_AFTER if recent else RETRY_AFTER[-1:]  # history: one more try next day
    n = lookup.attempts or 1
    if n <= len(schedule):
        lookup.next_try_at = now + schedule[n - 1]
        if error:
            lookup.status = "pending"
    else:
        lookup.next_try_at = None
        if error:
            lookup.status = "error"
    if error:
        lookup.detail = {**(lookup.detail or {}), "error": error[:255]}


# ------------------------------------------------------------------------------ queue


def enqueue(session: Session, study_ids, reset: bool = False) -> int:
    """Queue studies for a lookup (existing rows are left alone unless `reset`)."""
    n = 0
    for sid in study_ids:
        row = session.get(HisLookup, sid)
        if row is None:
            session.add(HisLookup(study_id=sid, status="pending", attempts=0))
            n += 1
        elif reset:
            row.status, row.next_try_at, row.attempts = "pending", None, 0
            n += 1
    session.flush()
    return n


def _in_hours(spec: str | None, now: dt.datetime) -> bool:
    if not spec:
        return True
    a, b = (int(x) for x in spec.split("-"))
    h = now.hour
    return a <= h < b if a < b else (h >= a or h < b)


def due(session: Session, server: HisServer, now: dt.datetime, limit: int) -> list[HisLookup]:
    """Pending lookups to do now: recent studies first, history only in backfill hours."""
    base = (select(HisLookup).join(Study, Study.id == HisLookup.study_id)
            .where(HisLookup.status == "pending",
                   (HisLookup.next_try_at.is_(None)) | (HisLookup.next_try_at <= now)))
    rows = list(session.scalars(base.where(Study.study_datetime >= now - RECENT)
                                .order_by(Study.study_datetime.desc()).limit(limit)))
    if len(rows) < limit and _in_hours(server.backfill_hours, now):
        rows += list(session.scalars(
            base.where((Study.study_datetime < now - RECENT) | Study.study_datetime.is_(None))
            .order_by(Study.study_datetime.desc()).limit(limit - len(rows))))
    return rows


def run_batch(session: Session, secret: bytes, *, limit: int = 20, dry_run: bool = False,
              now: dt.datetime | None = None, client=None, sleep=time.sleep,
              report=None) -> Counter:
    """Process up to `limit` due lookups with the first enabled server."""
    from ..ingest.store import derive

    stats: Counter = Counter()
    active = servers.active(session)
    if not active:
        stats["no_server"] = 1
        return stats
    server = active[0]
    now = now or dt.datetime.now()
    rows = due(session, server, now, limit)
    if not rows:
        return stats
    client = client or servers.client(server, secret)
    client.verify_login()  # a "not found" means something only after this
    ctx = Context()
    for row in rows:
        study_when = session.scalar(select(Study.study_datetime)
                                    .where(Study.id == row.study_id))
        calls = client.calls
        try:
            with session.begin_nested():
                status = process(session, row, server, client, ctx, now)
                if ctx.retry:
                    _retry(row, study_when, now)
                    if row.next_try_at is not None:
                        row.status = "pending"  # try again later: forms come late
                if status in ("matched", "patient_only") and not dry_run:
                    derive(session, [row.study_id])
            stats[status] += 1
        except HisError as e:
            if e.kind == "auth":
                raise
            row.attempts = (row.attempts or 0) + 1
            _retry(row, study_when, now, error=str(e))
            stats["error"] += 1
            log.warning("HIS lookup of study %s: %s", row.study_id, e)
        if report:
            report(row, stats)
        if dry_run:
            session.rollback()
        else:
            session.commit()
        if ctx.not_found_streak >= NOT_FOUND_RECHECK:
            client.verify_login()
            ctx.not_found_streak = 0
        sleep(server.min_interval_s * max(1, client.calls - calls))
    stats["calls"] = client.calls
    return stats
