"""Read-only queries behind the JSON API. Response shapes match web/data/sample/*.json."""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict

from sqlalchemy import and_, asc, desc, func, select
from sqlalchemy.orm import Session

from ..db.models import Device, Event, Patient, Study
from .query import conditions, parse

YEAR_DAYS = 365.25
STALE_DAYS = 2
GROUPS = [("CT", "CT", ["CT"]), ("DX", "DX/CR", ["DX", "CR"]), ("RF", "RF/XA", ["RF", "XA"]),
          ("MG", "MG", ["MG"])]
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


def overview(session: Session, now: dt.datetime) -> dict:
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
    modalities = []
    for code, label, mods in GROUPS:
        week = total(mods, today - dt.timedelta(days=7), today)
        prev = total(mods, today - dt.timedelta(days=14), today - dt.timedelta(days=7))
        weekly = [total(mods, today - dt.timedelta(days=7 * (i + 1)),
                        today - dt.timedelta(days=7 * i)) for i in reversed(range(12))]
        modalities.append({
            "code": code, "label": label, "week": week,
            "month": total(mods, today - dt.timedelta(days=30), today),
            "delta_pct": round(100 * (week - prev) / prev) if prev else 0, "weekly": weekly,
        })
    from .. import alerts

    return {"generated": iso_min(now), "modalities": modalities,
            "alerts": alerts.listing(session, "open", 20, names=device_names(session)),
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
            else None, "modality_override": d.modality_override,
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
    "dap_total": Study.total_dap,
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
    names = device_names(session)
    return {
        "total": total,
        "rows": [_study_row(s, d, p, protocols.get(s.id), names) for s, d, p in rows],
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


def _patient_summary(s: Study, p: Patient | None) -> dict:
    return {"age": age_years(s.age_days), "age_days": s.age_days,
            "age_precision": str(s.age_precision) if s.age_precision else None,
            "sex": p.sex if p else None, "weight": s.weight_kg,
            "weight_source": str(s.weight_source) if s.weight_source else None,
            "pseudo_id": p.pseudo_id if p else None}


def _study_row(s: Study, d: Device | None, p: Patient | None, protocol: str | None,
               names: dict[int, str]) -> dict:
    return {
        "id": s.id, "accession": accession_display(s), "accession_hashed": s.accession_is_hashed,
        "modality": (d.modality_override if d and d.modality_override else s.modality),
        "modality_original": s.modality,
        "date": iso_min(s.study_datetime), "device_id": (d.merged_into_id or d.id) if d else None,
        "device_name": names.get(d.id, "—") if d else "—",
        "description": s.study_description or "", "protocol": protocol,
        "patient": _patient_summary(s, p),
        "events": {"spiral": s.n_spiral, "axial": s.n_axial, "localizer": s.n_localizer,
                   "total": s.n_events},
        "ctdivol_max": _r(s.ctdivol_max, 2), "dlp_total": _r(s.total_dlp, 1),
        "dap_total": _r(s.total_dap), "flags": [],
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
        "modality": s.modality, "description": s.study_description or "",
        "start": iso_min(min(times) if times else s.study_datetime),
        "end": iso_min(max(times) if times else s.study_datetime),
        "device": {"id": (d.merged_into_id or d.id) if d else None,
                   "name": device_names(session).get(d.id) if d else None,
                   "manufacturer": d.manufacturer if d else None,
                   "model": d.model_name if d else None, "biplane": len(planes) > 1},
        "physician": s.performing_physician, "operator": s.operator_name,
        "requested_procedure": s.requested_procedure, "procedure": s.procedure_name,
        "patient": {**_patient_summary(s, p),
                    "height_cm": round(s.height_m * 100) if s.height_m else None,
                    "weight_kg": s.weight_kg},
        "totals": {
            "dap_gycm2": _r(s.total_dap), "kar_gy": _r(s.total_rp_dose),
            "fluoro_s": _r(s.total_fluoro_time, 1), "acquisitions": len(acq),
            "fluoro_events": len(fluoro), "frames": None, "peak_skin_mgy": None,
            "dlp_mgycm": _r(s.total_dlp, 1), "ctdivol_max": _r(s.ctdivol_max, 2),
            "n_events": s.n_events,
        },
        "thresholds": XA_THRESHOLDS if s.modality in ("RF", "XA") else None,
        "alerts": [],
        "previous": _previous(session, s) if p else [],
        "events": [_event(i + 1, e) for i, e in enumerate(events)],
    }


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
        "scan_length_mm": _r(e.scan_length_mm, 1), "pitch": _r(e.pitch, 3),
        "localizer": e.is_localizer, "exposure_index": _r(e.exposure_index, 1),
        "deviation_index": _r(e.deviation_index, 2), "agd_mgy": _r(e.agd, 3),
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
    """Non-localizer CT events of the studies matching q (keys: date device proto origin
    weight and the default test-patient exclusion), one row per acquisition."""
    allowed = {"date", "device", "proto", "origin", "weight", "age", "modality", "phantom"}
    tokens = [t for t in parse(q) if t.key in allowed]
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


def identify(session: Session, study_id: int) -> dict | None:
    row = session.execute(select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
                          .where(Study.id == study_id)).first()
    if row is None:
        return None
    s, p = row
    return {"study_id": s.id, "accession": s.accession,
            "name": p.name if p else None, "id": p.patient_id if p else None,
            "birth_date": p.birth_date.isoformat() if p and p.birth_date else None}


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
                min_n: int) -> dict:
    """GET /api/drl/compare – local medians against a reference set."""
    from .. import drl as drl_mod

    conds = conditions(parse(q), now, overrides(session))
    out = drl_mod.compare(session, conds, set_key, by_device=(by == "device"), min_n=min_n,
                          physical=_physical_ids(session), names=device_names(session))
    out["q"] = q
    return out
