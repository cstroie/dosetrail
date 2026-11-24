"""Read-only queries behind the JSON API. Response shapes match web/data/sample/*.json."""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from sqlalchemy import and_, asc, desc, func, select
from sqlalchemy.orm import Session

from .. import modality_profiles
from ..db.models import Device, Event, Patient, SizeSource, Study
from ..hybrid import label as modality_label
from ..patient_age import format_age
from .query import conditions, parse


class QueryError(ValueError):
    """A filter the page cannot apply (typo, bad date): said so, not silently dropped."""


def checked_conditions(q: str | None, now: dt.datetime, overrides=None) -> list:
    """``conditions`` for reports, exports and statistics: a token that does not parse is an
    error, as dropping it would answer for a wider population than the one asked for. (The
    study list shows the errors beside the results instead, as one types.)"""
    tokens = parse(q)
    _refuse_errors(tokens)
    return conditions(tokens, now, overrides)


def _refuse_errors(tokens: list) -> None:
    bad = [f"{t.key}:{t.value} ({t.error})" if t.value else f"{t.key} ({t.error})"
           for t in tokens if t.error]
    if bad:
        raise QueryError("query not understood: " + "; ".join(bad))


YEAR_DAYS = 365.25
STALE_DAYS = 2
GROUPS = modality_profiles.GROUPS
# Fluoroscopy alert thresholds: configuration defaults until the alert engine exists (M5)
XA_THRESHOLDS = {"dap_gycm2": 250, "kar_gy": 3.0, "kar_notify_gy": 1.0, "cum_kar_gy": 5.0,
                 "cum_dap_gycm2": 500, "cum_weeks": 12, "source": "default"}


def iso_min(t: dt.datetime | None) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M") if t else None


def iso_sec(t: dt.datetime | None) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%S") if t else None


def age_years(days: int | None) -> int | None:
    return None if days is None else int(days // YEAR_DAYS)


def accession_display(study: Study) -> str | None:
    if not study.accession:
        return None
    if study.accession_is_hashed:
        return "#" + study.accession[:10] + "…"
    return study.accession


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


# --------------------------------------------------------------------------- overview


def overview(session: Session, now: dt.datetime, specs_dir=None) -> dict:
    day0 = (now - dt.timedelta(days=84)).date()
    day = func.date(Study.study_datetime)
    counts: dict[tuple[str, dt.date], int] = {}
    q = (select(Study.modality, day, func.count())
         .where(Study.study_datetime >= dt.datetime.combine(day0, dt.time()))
         .group_by(Study.modality, day))
    for mod, d, n in session.execute(q):
        d = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
        counts[(mod, d)] = n

    def total(mods, start, end):  # [start, end) dates
        return sum(n for (m, d), n in counts.items() if m in mods and start <= d < end)

    today = now.date() + dt.timedelta(days=1)
    # only the modalities the local devices actually produce (no MG tile without a mammograph)
    present = set(session.scalars(
        select(Study.modality).join(Device, Device.id == Study.device_id)
        .where(Device.is_local.is_(True)).distinct()))
    modalities = []
    for code, label, mods in GROUPS:
        if not present.intersection(mods):
            continue
        week = total(mods, today - dt.timedelta(days=7), today)
        prev = total(mods, today - dt.timedelta(days=14), today - dt.timedelta(days=7))
        weekly = [total(mods, today - dt.timedelta(days=7 * (i + 1)),
                        today - dt.timedelta(days=7 * i)) for i in reversed(range(12))]
        modalities.append({
            "code": code, "label": label, "modalities": mods, "week": week,
            "month": total(mods, today - dt.timedelta(days=30), today),
            "delta_pct": round(100 * (week - prev) / prev) if prev else 0, "weekly": weekly,
        })
    from .. import alerts, deadlines
    from ..db.models import AppSetting
    from ..specs import loader

    marked = session.get(AppSetting, deadlines.DONE_KEY)
    return {"generated": iso_min(now), "modalities": modalities,
            "deadlines": deadlines.upcoming(
                now.date(), (marked.value if marked else None) or [],
                list(loader.discover(specs_dir).specs.values())),
            "alerts": alerts.listing(session, "open", 10, names=device_names(session)),
            "alerts_open": alerts.open_count(session),
            "feeds": feeds(session, now)}


def overrides(session: Session) -> dict[int, str]:
    """device id -> modality override (few or none; applied at query time)."""
    return dict(session.execute(select(Device.id, Device.modality_override)
                                .where(Device.modality_override.is_not(None))).all())


def device_names(session: Session) -> dict[int, str]:
    """device id -> display name of its physical device (aliases show the merged name)."""
    devs = {d.id: d for d in session.scalars(select(Device))}
    out = {}
    for i, d in devs.items():
        p = devs.get(d.merged_into_id, d) if d.merged_into_id else d
        out[i] = p.display_name or p.station_name or f"Device {p.id}"
    return out


def _physical_ids(session: Session) -> dict[int, int]:
    """device id -> physical (merge target) id."""
    return {i: (m or i) for i, m in session.execute(select(Device.id, Device.merged_into_id))}


def feeds(session: Session, now: dt.datetime, limit: int = 20) -> list[dict]:
    phys = _physical_ids(session)
    devs = {d.id: d for d in session.scalars(select(Device).where(Device.is_local.is_(True)))}
    last: dict[int, dt.datetime] = {}
    volume: Counter = Counter()
    for d in devs.values():
        p = phys[d.id]
        if d.last_study_at and (p not in last or d.last_study_at > last[p]):
            last[p] = d.last_study_at
        volume[p] += d.n_studies or 0
    days = [(now - dt.timedelta(days=i)).date() for i in reversed(range(7))]
    day = func.date(Study.study_datetime)
    per_day: Counter = Counter()
    q = (select(Study.device_id, day, func.count())
         .where(Study.study_datetime >= dt.datetime.combine(days[0], dt.time()),
                Study.device_id.in_(list(devs)))
         .group_by(Study.device_id, day))
    for did, d, n in session.execute(q):
        d = d if isinstance(d, dt.date) else dt.date.fromisoformat(str(d))
        per_day[(phys[did], d)] += n
    out = []
    for p, _n in volume.most_common(limit):
        if p not in devs or not volume[p]:
            continue
        lt = last.get(p)
        stale = lt is None or (now - lt).days >= STALE_DAYS
        out.append({
            "device_id": p, "name": devs[p].display_name or devs[p].station_name or f"#{p}",
            "last_study": iso_min(lt), "status": "stale" if stale else "ok",
            "days": [{"date": d.isoformat(), "n": per_day[(p, d)]} for d in days],
        })
    return out


# ---------------------------------------------------------------------------- devices


def devices(session: Session) -> dict:
    mod_counts: dict[int, Counter] = defaultdict(Counter)
    for did, mod, n in session.execute(select(Study.device_id, Study.modality, func.count())
                                       .group_by(Study.device_id, Study.modality)):
        if did is not None:
            mod_counts[did][mod] = n
    records = []
    for d in session.scalars(select(Device).order_by(Device.n_studies.desc(), Device.id)):
        mods = mod_counts.get(d.id)
        records.append({
            "id": d.id, "physical_id": d.merged_into_id or d.id,
            "display_name": d.display_name or d.station_name or f"Device {d.id}",
            "manufacturer": d.manufacturer, "model": d.model_name, "serial": d.serial_number,
            "station": d.station_name, "software": d.software_versions or "",
            "institution": d.institution_name, "modality": mods.most_common(1)[0][0] if mods
            else None, "modality_override": d.modality_override, "his_policy": d.his_policy,
            "origin": "local" if d.is_local else "external", "studies": d.n_studies or 0,
            "last_study": iso_min(d.last_study_at),
        })
    from .admin import dismissed

    skip = dismissed(session)
    sugg = [x for x in merge_suggestions(records) if tuple(sorted(x["record_ids"])) not in skip]
    return {"records": records, "merge_suggestions": sugg}


def merge_suggestions(records: list[dict]) -> list[dict]:
    """Group unmerged records that are probably the same physical unit."""
    rules = [
        ("same manufacturer (case-insensitive), model and serial",
         lambda r: (r["manufacturer"], r["model"], r["serial"]) if r["serial"] else None),
        ("same manufacturer, model, station and display name",
         lambda r: (r["manufacturer"], r["model"], r["station"], r["display_name"])),
    ]
    out, used = [], set()
    for text, keyf in rules:
        groups: dict = defaultdict(list)
        for r in records:
            if r["id"] != r["physical_id"] or r["id"] in used:
                continue
            k = keyf(r)
            if k is not None:
                groups[tuple((x or "").casefold().strip() for x in k)].append(r["id"])
        for ids in groups.values():
            if len(ids) > 1:
                out.append({"record_ids": ids, "rule": text})
                used.update(ids)
    return out


def drl() -> dict:
    # No local DRL set or protocol-to-exam mapping exists yet; nothing is invented here.
    return {"set": None, "ct_ctdivol_mgy": {}, "ct_dlp_mgycm": {}}


# ---------------------------------------------------------------------------- studies

SORTS = {
    "date": Study.study_datetime, "device": Device.display_name, "accession": Study.accession,
    "patient": Study.age_days, "description": Study.study_description,
    "events": Study.n_events, "dlp_total": Study.total_dlp, "ctdivol_max": Study.ctdivol_max,
    "dap_total": Study.total_dap, "n_events": Study.n_events,
    "fluoro_time": Study.total_fluoro_time, "fluoro_dap": Study.total_fluoro_dap,
    "acq_dap": Study.total_acq_dap, "kar_total": Study.total_rp_dose, "psd": Study.psd_mgy,
    "agd_left": Study.agd_left, "agd_right": Study.agd_right,
    "activity": Study.admin_activity_mbq,
}


def _base():
    return (select(Study, Device, Patient)
            .outerjoin(Device, Device.id == Study.device_id)
            .outerjoin(Patient, Patient.id == Study.patient_id))


def studies(session: Session, q: str, sort: str, direction: str, page: int, size: int,
            now: dt.datetime) -> dict:
    tokens = parse(q)
    conds = conditions(tokens, now, overrides(session))
    total = session.scalar(select(func.count(Study.id)).where(*conds))
    col = SORTS.get(sort, Study.study_datetime)
    order = desc if direction != "asc" else asc
    rows = session.execute(_base().where(*conds).order_by(order(col), order(Study.id))
                           .limit(size).offset((page - 1) * size)).all()
    protocols = _protocols(session, [s.id for s, _, _ in rows])
    technique = _technique(session, [s.id for s, _, _ in rows])
    names = device_names(session)
    return {
        "total": total,
        "rows": [_study_row(s, d, p, protocols.get(s.id), names, technique.get(s.id))
                 for s, d, p in rows],
        "errors": [{"token": t.key, "error": t.error} for t in tokens if t.error],
    }


def _protocols(session: Session, ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    if not ids:
        return out
    q = (select(Event.study_id, Event.protocol).where(Event.study_id.in_(ids),
                                                      Event.is_localizer.is_(False),
                                                      Event.protocol.is_not(None))
         .order_by(Event.study_id, Event.event_datetime, Event.id))
    for sid, proto in session.execute(q):
        out.setdefault(sid, proto)
    return out


def _technique(session: Session, ids: list[int]) -> dict[int, dict]:
    """Per-study ranges of projection technique (kV, EI, DI) for the list columns."""
    if not ids:
        return {}
    q = (select(Event.study_id, func.max(Event.kvp), func.min(Event.exposure_index),
                func.max(Event.exposure_index), func.min(Event.deviation_index),
                func.max(Event.deviation_index))
         .where(Event.study_id.in_(ids)).group_by(Event.study_id))
    return {sid: {"kvp_max": _r(kv, 1), "ei_min": _r(e0, 0), "ei_max": _r(e1, 0),
                  "di_min": _r(d0, 1), "di_max": _r(d1, 1)}
            for sid, kv, e0, e1, d0, d1 in session.execute(q)}


def _patient_summary(s: Study, p: Patient | None) -> dict:
    return {"age": age_years(s.age_days), "age_days": s.age_days,
            "age_precision": str(s.age_precision) if s.age_precision else None,
            "sex": p.sex if p else None, "weight": s.weight_kg,
            "weight_source": str(s.weight_source) if s.weight_source else None,
            "weight_estimated": s.weight_source == SizeSource.estimated_reference,
            "weight_reported": s.weight_reported_kg,
            "pseudo_id": p.pseudo_id if p else None}


def _study_row(s: Study, d: Device | None, p: Patient | None, protocol: str | None,
               names: dict[int, str], technique: dict | None = None) -> dict:
    tech = technique or {}
    modality = d.modality_override if d and d.modality_override else s.modality
    return {
        "id": s.id, "accession": accession_display(s), "accession_hashed": s.accession_is_hashed,
        "modality": modality, "group": modality_profiles.group_of(modality),
        "modality_original": s.modality,
        "modality_label": modality_label(s.modality, s.total_dlp),
        "date": iso_min(s.study_datetime), "device_id": (d.merged_into_id or d.id) if d else None,
        "device_name": names.get(d.id, "—") if d else "—",
        "description": s.study_description or "", "protocol": protocol,
        "patient": _patient_summary(s, p),
        "events": {"spiral": s.n_spiral, "axial": s.n_axial, "localizer": s.n_localizer,
                   "total": s.n_events},
        "ctdivol_max": _r(s.ctdivol_max, 2), "dlp_total": _r(s.total_dlp, 1),
        "dap_total": _r(s.total_dap), "n_events": s.n_events,
        "fluoro_time": _r(s.total_fluoro_time, 1), "fluoro_dap": _r(s.total_fluoro_dap),
        "acq_dap": _r(s.total_acq_dap), "kar_total": _r(s.total_rp_dose),
        "psd": _r(s.psd_mgy, 1), "agd_left": _r(s.agd_left, 2), "agd_right": _r(s.agd_right, 2),
        "activity": _r(s.admin_activity_mbq, 1), **tech, "flags": [],
        "test": bool(p.is_test) if p else False,
    }


def study(session: Session, study_id: int) -> dict | None:
    row = session.execute(_base().where(Study.id == study_id)).first()
    if row is None:
        return None
    s, d, p = row
    events = list(session.scalars(select(Event).where(Event.study_id == s.id)
                                  .order_by(Event.event_datetime, Event.id)))
    times = [e.event_datetime for e in events if e.event_datetime]
    fluoro = [e for e in events if e.event_type == "fluoro"]
    acq = [e for e in events if e.event_type != "fluoro"]
    planes = {e.plane for e in events if e.plane}
    return {
        "id": s.id, "accession": accession_display(s), "accession_hashed": s.accession_is_hashed,
        "modality": s.modality, "modality_label": modality_label(s.modality, s.total_dlp),
        "description": s.study_description or "", "contrast": s.contrast_mode,
        "start": iso_min(min(times) if times else s.study_datetime),
        "end": iso_min(max(times) if times else s.study_datetime),
        "device": {"id": (d.merged_into_id or d.id) if d else None,
                   "name": device_names(session).get(d.id) if d else None,
                   "manufacturer": d.manufacturer if d else None,
                   "model": d.model_name if d else None, "biplane": len(planes) > 1},
        "physician": s.performing_physician, "operator": s.operator_name,
        "requested_procedure": s.requested_procedure, "procedure": s.procedure_name,
        "glandularity_pct": s.glandularity_pct,
        "patient": {**_patient_summary(s, p),
                    "age_display": format_age(s.age_days) if s.age_days is not None else None,
                    "height_cm": round(s.height_m * 100) if s.height_m else None,
                    "weight_kg": s.weight_kg},
        "totals": {
            "dap_gycm2": _r(s.total_dap), "kar_gy": _r(s.total_rp_dose),
            "fluoro_s": _r(s.total_fluoro_time, 1), "acquisitions": len(acq),
            "fluoro_events": len(fluoro), "frames": None, "peak_skin_mgy": _r(s.psd_mgy, 1),
            "dlp_mgycm": _r(s.total_dlp, 1), "ctdivol_max": _r(s.ctdivol_max, 2),
            "n_events": s.n_events, "activity_mbq": _r(s.admin_activity_mbq, 1),
            "agd_left_mgy": _r(s.agd_left, 2), "agd_right_mgy": _r(s.agd_right, 2),
            # the administration this study shares, counted on that earlier study
            "activity_counted_on": next((x for e in events if (x := (e.extra_json or {})
                                         .get("same_administration_as"))), None),
        },
        "thresholds": XA_THRESHOLDS if s.modality in ("RF", "XA") else None,
        "alerts": _alerts(session, s.id),
        "his": his_summary(session, s.id),
        "exams": _exams(session, s.id),
        "previous": _previous(session, s) if p else [],
        "events": _with_references(session, s, events,
                                   [_event(i + 1, e) for i, e in enumerate(events)]),
    }


def his_summary(session: Session, study_id: int) -> dict | None:
    """The HIS lookup outcome and the matched request (no patient identifiers)."""
    from ..db.models import HisLookup, StudyRequest

    lk = session.get(HisLookup, study_id)
    if lk is None:
        return None
    d = lk.detail or {}
    r = session.get(StudyRequest, study_id)
    return {
        "status": lk.status, "attempts": lk.attempts, "score": lk.match_score,
        "next_try": iso_min(lk.next_try_at), "checked": iso_min(lk.fetched_at),
        "written": d.get("written") or [], "rejected": d.get("rejected") or [],
        "error": d.get("error"), "skipped": d.get("why"),
        "other_person": bool(d.get("rejected_patient") or d.get("rejected_request")),
        "request": None if r is None else {
            "id": r.request_id, "ordered": iso_min(r.authored_on), "ward": r.ward,
            "requester": r.requester, "priority": r.priority, "procedure": r.procedure,
            "region": r.region, "indication": r.indication, "guideline": r.guideline,
            "prior_imaging": r.prior_imaging, "pregnant": r.pregnant,
            "weight_kg": r.weight_kg, "height_m": r.height_m},
    }


def _alerts(session: Session, study_id: int) -> list[dict]:
    from ..db.models import Alert, AlertRule

    rows = session.execute(select(Alert, AlertRule).join(AlertRule, AlertRule.id == Alert.rule_id)
                           .where(Alert.study_id == study_id).order_by(Alert.id))
    return [{"id": a.id, "level": a.severity, "kind": r.kind, "rule": r.key, "name": r.name,
             "text": a.message, "status": a.status, "ack_by": a.ack_by}
            for a, r in rows]


def _exams(session: Session, study_id: int) -> list[dict]:
    from ..db.models import Classification, Code, CodeSystem

    rows = session.execute(
        select(CodeSystem.key, CodeSystem.name, Code.code, Code.meaning)
        .join(Code, Code.system_id == CodeSystem.id)
        .join(Classification, Classification.code_id == Code.id)
        .where(Classification.study_id == study_id, Classification.event_id.is_(None))
        .order_by(CodeSystem.key, Code.code)).all()
    return [{"system": k, "system_name": n, "code": c, "meaning": m} for k, n, c, m in rows]


def _with_references(session: Session, s: Study, events: list, rows: list[dict]) -> list[dict]:
    """Per event: its exam code and the DRL that applies to it (CT series: DLP and CTDIvol;
    radiography exposure: the projection's DAP level, which the projection sums up to)."""
    from ..db.models import Classification, Code, CodeSystem
    from ..reference import CTReference
    from ..stats import BandLookup

    ids = [e.id for e in events]
    own: dict[int, dict[str, str]] = {}
    for eid, system, code in session.execute(
            select(Classification.event_id, CodeSystem.key, Code.code)
            .join(Code, Code.id == Classification.code_id).join(CodeSystem)
            .where(Classification.event_id.in_(ids or [-1]))):
        own.setdefault(eid, {})[system] = code
    if s.modality == "CT":
        ref = CTReference(session)
        study_codes = ref.study_codes([s.id]).get(s.id, {})
        for e, r in zip(events, rows, strict=True):
            if e.is_localizer:
                continue
            codes = own.get(e.id) or study_codes
            d = ref.for_series(codes, s.age_days, s.weight_kg)
            r["exam"] = codes.get("ro-drl-ms838-2025") or codes.get("ec-rp185")
            r["drl"] = {"dlp": d["dlp"], "ctdivol": d["ctdivol"], "source": d["source"]}
        return rows
    if s.modality in ("DX", "CR", "MG"):
        from ..quality import Checker

        drl = Checker(session).drl
        band = BandLookup(session, "ro-drl-age", "age")(s.age_days)
        from ..classify import FRONTAL_BY_DEFAULT, view_code

        for e, r in zip(events, rows, strict=True):
            codes = own.get(e.id, {})
            r["exam"] = codes.get("ro-nom-rx")
            r["projection"] = view_code(e.view, e.protocol) or (
                "AP" if r["exam"] in FRONTAL_BY_DEFAULT else None)
            nat = codes.get("ro-drl-ms838-2025")
            value = drl.get((nat, band, "dap")) if nat else None
            r["drl"] = {"dap": value, "source": {"dap": f"OMS 838/2025 {nat}"} if value else {},
                        "code": nat}
    return rows


def _event(no: int, e: Event) -> dict:
    return {
        "no": no, "uid": e.event_uid, "time": iso_sec(e.event_datetime),
        "type": e.event_type, "protocol": e.protocol, "plane": e.plane,
        "anatomy": e.anatomy, "laterality": e.laterality, "view": e.view,
        "kvp": _r(e.kvp, 1), "ma": _r(e.ma, 1), "mas": _r(e.mas, 2),
        "exposure_time_s": _r(e.exposure_time_s, 4), "pulse_rate": _r(e.pulse_rate, 1),
        "cu_mm": _r(e.filter_cu_mm, 2), "al_mm": _r(e.filter_al_mm, 2),
        "primary_angle": _r(e.primary_angle, 1), "secondary_angle": _r(e.secondary_angle, 1),
        "field_cm": _r(e.field_w_mm / 10, 1) if e.field_w_mm else None,
        "sid_cm": _r(e.sid_mm / 10, 1) if e.sid_mm else None,
        "dap_gycm2": _r(e.dap, 6),
        "kar_mgy": _r(e.rp_dose * 1000, 3) if e.rp_dose is not None else None,
        "duration_s": _r(e.fluoro_time_s if e.fluoro_time_s is not None else e.exposure_time_s,
                         3),
        "ctdivol": _r(e.ctdivol, 2), "dlp": _r(e.dlp, 1), "phantom_cm": e.ct_phantom_cm,
        "ssde_mgy": _r(e.ssde if e.ssde is not None else e.ssde_calc, 2),
        "ssde_source": "reported" if e.ssde is not None else e.ssde_method,
        "wed_cm": _r(e.wed_mm / 10, 1) if e.wed_mm else None,
        "scan_length_mm": _r(e.scan_length_mm, 1), "pitch": _r(e.pitch, 3),
        "localizer": e.is_localizer, "exposure_index": _r(e.exposure_index, 1),
        "deviation_index": _r(e.deviation_index, 2), "agd_mgy": _r(e.agd, 3),
        # mammography
        "esd_mgy": _r(e.esd, 3), "thickness_mm": _r(e.compression_thickness_mm, 0),
        "force_n": _r(e.compression_force_n, 0), "target": e.target_material,
        "filter": e.filter_material,
        # radiopharmaceutical administration (NM/PET): agent in protocol, nuclide in comment
        **({"agent": e.protocol, "nuclide": e.comment,
            "activity_mbq": _r((e.extra_json or {}).get("activity_mbq"), 1),
            "route": (e.extra_json or {}).get("route"),
            "same_administration_as": (e.extra_json or {}).get("same_administration_as")}
           if e.event_type == "radiopharmaceutical" else {}),
    }


def _previous(session: Session, s: Study, weeks: int = 12) -> list[dict]:
    if not s.study_datetime or not s.patient_id:
        return []
    start = s.study_datetime - dt.timedelta(weeks=weeks)
    q = (select(Study).where(Study.patient_id == s.patient_id, Study.id != s.id,
                             and_(Study.study_datetime >= start,
                                  Study.study_datetime <= s.study_datetime))
         .order_by(Study.study_datetime))
    return [{"id": o.id, "accession": accession_display(o), "modality": o.modality,
             "date": o.study_datetime.date().isoformat(), "description": o.study_description,
             "dap_gycm2": _r(o.total_dap), "kar_gy": _r(o.total_rp_dose),
             "fluoro_s": _r(o.total_fluoro_time, 1), "dlp_mgycm": _r(o.total_dlp, 1)}
            for o in session.scalars(q)]


def ct_acquisitions(session: Session, q: str, now: dt.datetime, limit: int = 50000) -> list:
    """Non-localizer CT events of the studies matching q (keys: date device proto desc origin
    weight and the default test-patient exclusion), one row per acquisition."""
    allowed = {"date", "device", "proto", "desc", "origin", "weight", "age", "modality", "phantom"}
    tokens = [t for t in parse(q) if t.key in allowed]
    _refuse_errors(tokens)
    conds = conditions(tokens, now, overrides(session))
    phys = _physical_ids(session)
    names = device_names(session)
    rows = session.execute(
        select(Event.protocol, Event.ctdivol, Event.dlp, Event.event_datetime,
               Study.device_id, Study.weight_kg, Study.age_days, Study.study_datetime,
               Study.id, Event.id)
        .join(Study, Study.id == Event.study_id)
        .where(Study.modality == "CT", Event.is_localizer.is_(False), *conds)
        .order_by(Study.study_datetime.desc()).limit(limit)).all()
    from ..reference import CTReference

    ref = CTReference(session)
    codes = ref.study_codes(list({r[8] for r in rows}))
    own = ref.event_codes([r[9] for r in rows])  # series of another region than the study
    out = []
    for proto, ctdi, dlp, t, did, w, age, st, sid, eid in rows:
        pid = phys.get(did, did)
        drl = ref.for_series(own.get(eid) or codes.get(sid, {}), age, w)
        out.append({"protocol": proto or "(none)", "device_id": pid,
                    "device": names.get(pid), "ctdivol": _r(ctdi, 2), "dlp": _r(dlp, 1),
                    "weight": w, "age": age_years(age), "time": iso_min(t or st),
                    # the DRL that applies to this series (exam + patient group), or None
                    "drl": {"ctdivol": drl["ctdivol"], "dlp": drl["dlp"],
                            "source": drl["source"]}})
    return out


# per-exposure modalities chart one row per event, the others one row per study
_EVENT_FIELDS = {"dap": (Event.dap, 4), "kvp": (Event.kvp, 1), "mas": (Event.mas, 2),
                 "ei": (Event.exposure_index, 0), "di": (Event.deviation_index, 2),
                 "agd": (Event.agd, 3), "thickness": (Event.compression_thickness_mm, 0)}
_STUDY_FIELDS = {"dap_total": (Study.total_dap, 4), "kar_total": (Study.total_rp_dose, 4),
                 "fluoro_time": (Study.total_fluoro_time, 1), "psd": (Study.psd_mgy, 1),
                 "activity": (Study.admin_activity_mbq, 1)}


class _NationalDRL:
    """National DRLs (OMS 838/2025) in stored units, and the national exam code of each
    exposure (its own classification, else its study's), banded by the national age groups."""

    def __init__(self, session: Session, study_ids: list[int], event_ids: list[int]):
        from ..db.models import (
            Classification,
            Code,
            CodeSystem,
            GroupBand,
            ReferenceSet,
            ReferenceValue,
        )
        from ..drl import QUANTITY
        from ..stats import BandLookup

        self.values: dict[tuple, float] = {}
        rs = session.scalar(select(ReferenceSet).filter_by(key="ro-ms838-2025"))
        if rs is not None:
            for rv, code, band in session.execute(
                    select(ReferenceValue, Code.code, GroupBand.key)
                    .join(Code, Code.id == ReferenceValue.exam_code_id)
                    .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
                    .where(ReferenceValue.set_id == rs.id)):
                factor = QUANTITY.get(rv.quantity, (None, {}))[1].get(rv.unit)
                if factor:
                    self.values[(code, band, rv.quantity)] = rv.value / factor
        self.study: dict[int, str] = {}
        self.event: dict[int, str] = {}
        nat = (select(Classification.study_id, Classification.event_id, Code.code)
               .join(Code, Code.id == Classification.code_id).join(CodeSystem)
               .where(CodeSystem.key == "ro-drl-ms838-2025"))
        for chunk in range(0, len(study_ids), 5000):
            ids = list(set(study_ids[chunk:chunk + 5000]))
            for sid, eid, code in session.execute(nat.where(Classification.study_id.in_(ids))):
                if eid is None:
                    self.study.setdefault(sid, code)
                else:
                    self.event.setdefault(eid, code)
        self.band = BandLookup(session, "ro-drl-age", "age")

    def value(self, study_id: int, event_id: int | None, age_days: int | None,
              quantity: str) -> float | None:
        code = (self.event.get(event_id) if event_id else None) or self.study.get(study_id)
        if code is None:
            return None
        return self.values.get((code, self.band(age_days), quantity)) or \
            self.values.get((code, None, quantity))


def group_rows(session: Session, q: str, now: dt.datetime, group: str,
               limit: int = 50000) -> list:
    """Chart rows of one modality group (the browser chart panels): CT keeps its acquisition
    rows; DX/CR/MG one row per exposure; RF/XA/NM/PT one row per study. Each row carries
    protocol (the series protocol, else the study description), device, weight, age, time
    and the group's metric fields."""
    prof = next((p for p in modality_profiles.PROFILES if p["code"] == group.upper()), None)
    if prof is None:
        raise QueryError(f"unknown modality group {group!r}")
    if prof["code"] == "CT":
        return ct_acquisitions(session, q, now, limit)
    allowed = {"date", "device", "proto", "desc", "origin", "weight", "age", "phantom"}
    tokens = [t for t in parse(q) if t.key in allowed]
    _refuse_errors(tokens)
    conds = conditions(tokens, now, overrides(session))
    ov = overrides(session)
    mods = prof["modalities"]
    mod_cond = Study.modality.in_(mods)
    if ov:
        mod_cond = (mod_cond & Study.device_id.not_in(list(ov))) | Study.device_id.in_(
            [d for d, m in ov.items() if m in mods] or [-1])
    per_event = prof["code"] in ("DX", "MG")
    fields = _EVENT_FIELDS if per_event else _STUDY_FIELDS
    cols = [c for c, _ in fields.values()]
    base = [Study.device_id, Study.weight_kg, Study.age_days, Study.study_datetime,
            Study.study_description]
    if per_event:
        stmt = (select(Study.id, Event.id, *base, Event.protocol, Event.event_datetime, *cols)
                .join(Study, Study.id == Event.study_id))
    else:
        stmt = select(Study.id, Study.id.label("e"), *base, Study.procedure_name,
                      Study.study_datetime.label("t"), *cols)
    rows = session.execute(stmt.where(mod_cond, *conds)
                           .order_by(Study.study_datetime.desc()).limit(limit)).all()
    phys = _physical_ids(session)
    names = device_names(session)
    refs = _NationalDRL(session, [r[0] for r in rows], [r[1] for r in rows] if per_event else [])
    quantity = {"DX": ("dap", "dap"), "RF": ("dap_total", "dap"), "XA": ("dap_total", "dap"),
                "NM": ("activity", "activity"), "PT": ("activity", "activity")}.get(prof["code"])
    out = []
    for sid, eid, did, w, age, st, desc_, proto, t, *vals in rows:
        pid = phys.get(did, did)
        r = {"protocol": proto or desc_ or "(none)", "device_id": pid, "device": names.get(pid),
             "weight": w, "age": age_years(age), "time": iso_min(t or st), "drl": {}}
        for (k, (_, nd)), v in zip(fields.items(), vals, strict=True):
            r[k] = _r(v, nd)
        if "activity" in r:
            r["activity_kg"] = _r(r["activity"] / w, 2) if r["activity"] and w else None
        if "kar_total" in r and r["kar_total"] is not None:
            r["kar_total"] = _r(r["kar_total"] * 1000, 1)  # Gy → mGy, as the list shows it
        if quantity:  # the national DRL of this exposure's / study's exam and age group
            ref = refs.value(sid, eid if per_event else None, age, quantity[1])
            if ref is not None:
                r["drl"] = {quantity[0]: ref, "source": {quantity[0]: "OMS 838/2025"}}
        out.append(r)
    return out


def identify(session: Session, study_id: int) -> dict | None:
    row = session.execute(select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
                          .where(Study.id == study_id)).first()
    if row is None:
        return None
    s, p = row
    return {"study_id": s.id, "accession": s.accession,
            "name": p.name if p else None, "id": p.patient_id if p else None,
            "birth_date": p.birth_date.isoformat() if p and p.birth_date else None}


def dose_statement(session: Session, study_id: int) -> dict | None:
    """What the patient is told in writing with the result (Order 1255/2016 art. 4): who, which
    examination and the dose quantities that apply to it, in the units of the norm."""
    row = session.execute(select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
                          .where(Study.id == study_id)).first()
    if row is None:
        return None
    s, p = row
    esd = session.scalar(select(func.sum(Event.esd)).where(Event.study_id == s.id))
    calc = session.scalar(select(func.sum(Event.esak_calc)).where(Event.study_id == s.id))
    doses: list[dict] = []

    def add(key, label, value, unit, note=None):
        if value is not None:
            doses.append({"key": key, "label": label, "value": value, "unit": unit, "note": note})

    add("dap", "DAP", s.total_dap, "Gy·cm²")
    add("esak", "ESAK", esd if esd is not None else calc, "mGy",
        None if esd is not None else "calculated")
    add("rp", "Reference point dose (Ka,r)", s.total_rp_dose * 1000 if s.total_rp_dose is not None
        else None, "mGy")
    add("mgd_right", "MGD right breast", s.agd_right, "mGy")
    add("mgd_left", "MGD left breast", s.agd_left, "mGy")
    add("dlp", "DLP", s.total_dlp, "mGy·cm")
    add("activity", "Administered activity", s.admin_activity_mbq, "MBq")
    dev = session.get(Device, s.device_id) if s.device_id else None
    names = device_names(session)
    return {"study_id": s.id, "accession": s.accession, "modality": s.modality,
            "when": s.study_datetime.isoformat(timespec="minutes") if s.study_datetime else None,
            "exam": s.procedure_name or s.study_description,
            "device": names.get(s.device_id) if dev else None,
            "institution": (dev.institution_name if dev else None),
            "patient": {"name": p.name if p else None, "id": p.patient_id if p else None,
                        "birth_date": p.birth_date.isoformat() if p and p.birth_date else None},
            "doses": doses}


def lookup(session: Session, text: str, limit: int) -> list[dict]:
    t = text.strip()
    if len(t) < 3:
        return []
    if t.upper().startswith("P-"):
        if len(t) < 7:  # at least "P-XXXXX": a shorter prefix matches thousands of patients
            return []
        pids = list(session.scalars(select(Patient.id).where(
            Patient.pseudo_id.startswith(t.upper(), autoescape=True)).limit(limit)))
        cond = Study.patient_id.in_(pids)
    else:
        cond = Study.accession.startswith(t, autoescape=True)
    rows = session.execute(_base().where(cond).order_by(Study.study_datetime.desc())
                           .limit(limit)).all()
    names = device_names(session)
    out = []
    for s, d, _p in rows:
        dose = (f"{s.total_dlp:.0f} mGy·cm" if s.total_dlp else
                f"{s.total_dap:.3g} Gy·cm²" if s.total_dap else "")
        out.append({"id": s.id, "accession": accession_display(s), "modality": s.modality,
                    "description": s.study_description, "date": iso_min(s.study_datetime),
                    "device_name": names.get(d.id) if d else None,
                    "dose": dose})
    return out


def stats(session: Session, q: str, now: dt.datetime, metric: str, by: str, system: str | None,
          exam: str | None, age_scheme: str, weight_scheme: str, bins: int,
          min_n: int) -> dict:
    """GET /api/stats – one metric grouped by dimensions over the studies q selects."""
    from .. import stats as st

    tokens = parse(q)
    _refuse_errors(tokens)
    conds = conditions(tokens, now, overrides(session))
    if system is None:
        mods = {m.upper() for t in tokens if t.key == "modality" and not t.neg
                for m in t.value.split(",")}
        system = "ro-nom-ct" if mods == {"CT"} else "ro-nom-rx"
    group_by = [g.strip() for g in (by or "").split(",") if g.strip()]
    exams = [e.strip() for e in (exam or "").split(",") if e.strip()] or None
    out = st.compute(session, conds, metric, group_by, system=system, exams=exams,
                     age_scheme=age_scheme, weight_scheme=weight_scheme, bins=bins,
                     min_n=min_n, physical=_physical_ids(session), names=device_names(session))
    out["q"] = q
    return out


def drl_compare(session: Session, q: str, now: dt.datetime, set_key: str, by: str,
                min_n: int, estimated: bool = False) -> dict:
    """GET /api/drl/compare – local medians against a reference set."""
    from .. import drl as drl_mod

    conds = checked_conditions(q, now, overrides(session))
    out = drl_mod.compare(session, conds, set_key, by_device=(by == "device"), min_n=min_n,
                          physical=_physical_ids(session), names=device_names(session),
                          estimated_weights=estimated)
    out["q"] = q
    return out
