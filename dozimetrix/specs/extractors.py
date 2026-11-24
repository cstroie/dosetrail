"""Built-in row extractors: the row shapes of the national exports.

An extractor yields ``Row`` objects whose ``values`` dict holds the finished row (one key per
column of the spec). The spec owns the layout (which columns, in which order, with which unit and
description), the version and the parameters; the extractor owns the derivation of the numbers
(checks, groups, pseudonyms) that a column list cannot express. A site that needs another shape
registers its own with ``@extractor`` in a plugin.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterator

from sqlalchemy import select

from ..db.models import (
    Classification,
    Code,
    CodeSystem,
    Device,
    Event,
    Patient,
    Study,
    StudyIndication,
    StudyRequest,
)
from ..export.pseudo import (
    EUROPEAN,
    NATIONAL,
    NOMENCLATORS,
    _code,
    _r,
    coarse_age,
    device_code,
    patient_code,
)
from ..indications import USED
from ..patient_age import band_for_age, band_for_weight
from ..stats import load_bands
from .context import Context
from .extract import Row, window_conds
from .registry import extractor

EXPOSURE_EXAM = ("DX", "CR", "MG", "PX")
HEAD_CODES = {"CT-HEAD", "RX-HEAD-AP-PA", "RX-HEAD-LAT"}
BATCH = 2000


def _row(ctx: Context, s: Study, p, values: dict) -> Row:
    return Row(ctx, s, p, values=values)


# ----------------------------------------------------------------------------- survey


@extractor("survey_rows")
def survey_rows(ctx: Context) -> Iterator[Row]:
    """The national paediatric survey: CT one row per series, radiography one per projection,
    fluoroscopy one per study; paediatric only (``rules.max_age_years``)."""
    from .. import growth
    from ..classify import view_code

    session, secret = ctx.session, ctx.secret
    _, head_bands = load_bands(session, "rp185-head-age")
    _, weight_bands = load_bands(session, "ext-body-weight")
    max_age = ctx.params.get("max_age_years") or ctx.spec.rules.get("max_age_years", 18)
    max_days = int(float(max_age) * 365.25)
    fields = [c["header"] for c in ctx.spec.body["column"]]
    ids = list(session.scalars(
        select(Study.id).where(*window_conds(ctx), Study.age_days.is_not(None),
                               Study.age_days < max_days,
                               Study.modality.in_(["CT", "DX", "CR", "RF", "XA"]))
        .order_by(Study.study_datetime, Study.id)))
    devices = {d.id: d for d in session.scalars(select(Device))}
    physical = {i: (d.merged_into_id or i) for i, d in devices.items()}
    inst = _code(secret, "institution", ctx.params.get("institution") or "DEFAULT")
    tolerance = ctx.spec.rules.get("ct_dlp_tolerance", 0.1)
    growth_tables = growth.tables()
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = session.execute(
            select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.id.in_(chunk)).order_by(Study.study_datetime, Study.id)).all()
        study_codes: dict[int, dict[str, str]] = defaultdict(dict)
        event_codes: dict[int, dict[str, str]] = defaultdict(dict)
        for sid, eid, system, code in session.execute(
                select(Classification.study_id, Classification.event_id, CodeSystem.key,
                       Code.code).join(Code, Code.id == Classification.code_id)
                .join(CodeSystem).where(Classification.study_id.in_(chunk))):
            (study_codes[sid] if eid is None else event_codes[eid]).setdefault(system, code)
        indications = dict(session.execute(
            select(StudyRequest.study_id, StudyRequest.indication)
            .where(StudyRequest.study_id.in_(chunk))).all())
        ind_codes = dict(session.execute(
            select(StudyIndication.study_id, Code.code)
            .join(Code, Code.id == StudyIndication.code_id)
            .where(StudyIndication.study_id.in_(chunk), StudyIndication.status.in_(USED))
        ).all())
        events: dict[int, list[Event]] = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_(chunk),
                                                     Event.is_localizer.is_not(True))
                                 .order_by(Event.study_id, Event.event_datetime, Event.id)):
            events[e.study_id].append(e)
        for s, p in studies:
            d = devices.get(physical.get(s.device_id)) if s.device_id else None
            hb = band_for_age(head_bands, days=s.age_days)
            wb = band_for_weight(weight_bands, s.weight_kg) if s.weight_kg else None
            sc = study_codes.get(s.id, {})
            who = growth_tables.assess(p.sex if p else None, s.age_days, s.weight_kg) \
                if growth_tables else None
            base = {
                "survey_version": ctx.spec.version, "institution": inst,
                "device": device_code(secret, d),
                "manufacturer": d.manufacturer if d else None,
                "model": d.model_name if d else None, "modality": s.modality,
                "period": s.study_datetime.strftime("%Y-%m") if s.study_datetime else None,
                "patient": patient_code(secret, p),
                "sex": p.sex if p else None, **coarse_age(s.age_days),
                "head_age_group": hb.key if hb else None, "weight_kg": s.weight_kg,
                "weight_source": s.weight_source.value if s.weight_source else None,
                "weight_reported_kg": s.weight_reported_kg,
                "weight_group": wb.key if wb else None, "height_m": s.height_m,
                "indication": ind_codes.get(s.id),
                "indication_text": indications.get(s.id) or s.indication_text,
                "contrast": s.contrast_mode,
                "reconstruction": None, "kae": None,
                "weight_missing": not s.weight_kg,
                "weight_percentile": (who or {}).get("percentile"),
                "drl_exclusion": s.drl_exclusion,
            }
            evs = events.get(s.id, [])
            if s.modality == "CT":
                for n, e in enumerate(evs, 1):
                    yield _row(ctx, s, p, _survey_ct(fields, base, event_codes.get(e.id) or sc,
                                                     n, e, tolerance))
            elif s.modality in EXPOSURE_EXAM:
                groups: dict[tuple, list[Event]] = defaultdict(list)
                for e in evs:
                    ec = event_codes.get(e.id, {})
                    groups[(ec.get("ro-nom-rx"), ec.get(EUROPEAN), ec.get(NATIONAL),
                            view_code(e.view, e.protocol))].append(e)
                for (nom, eu, nat, view), g in groups.items():
                    yield _row(ctx, s, p, _survey_sum(fields, base, nom, eu, nat, view, g))
            else:
                yield _row(ctx, s, p, _survey_sum(fields, base, sc.get("ro-nom-rx"),
                                                  sc.get(EUROPEAN), sc.get(NATIONAL), None, evs))
        session.expunge_all()


def _survey_ct(fields, base: dict, sc: dict, n: int, e: Event, tolerance: float) -> dict:
    r = {**{f: None for f in fields}, **base}
    eu = sc.get(EUROPEAN)
    check = "n.a."
    if e.ctdivol and e.scan_length_mm and e.dlp:
        expected = e.ctdivol * e.scan_length_mm / 10
        check = "ok" if abs(expected - e.dlp) <= tolerance * e.dlp else "fail"
    phantom = "n.a."
    if e.ct_phantom_cm:
        phantom = "warn" if (eu in HEAD_CODES and e.ct_phantom_cm == 32) else "ok"
    r.update({"exam_code": sc.get("ro-nom-ct"), "edrl_exam": eu, "ndrl_exam": sc.get(NATIONAL),
              "series": n, "protocol": e.protocol, "ctdivol": _r(e.ctdivol, 3),
              "phantom": e.ct_phantom_cm, "dlp": _r(e.dlp, 3),
              "scan_length": _r(e.scan_length_mm, 1), "kvp": e.kvp, "mas": _r(e.mas, 2),
              "effective_mas": _r(e.mas / e.pitch, 2) if e.mas and e.pitch else None,
              "pitch": e.pitch, "tcm": e.tcm,
              "ssde": _r(e.ssde if e.ssde is not None else e.ssde_calc, 3), "exposures": 1,
              "check_dlp": check, "check_phantom": phantom})
    return r


def _survey_sum(fields, base, nom, eu, nat, view, evs: list[Event]) -> dict:
    r = {**{f: None for f in fields}, **base}

    def total(attr, factor=1.0):
        v = [getattr(e, attr) for e in evs if getattr(e, attr) is not None]
        return _r(sum(v) * factor, 4) if v else None

    kvp = [e.kvp for e in evs if e.kvp is not None]
    r.update({"exam_code": nom, "edrl_exam": eu, "ndrl_exam": nat, "projection": view,
              "protocol": evs[0].protocol if evs else None,
              "kvp": _r(sum(kvp) / len(kvp), 1) if kvp else None, "mas": total("mas"),
              "pka": total("dap", 1000.0), "fluoro_time": total("fluoro_time_s"),
              "exposures": len(evs), "check_dlp": "n.a.", "check_phantom": "n.a."})
    return r


# ----------------------------------------------------------------------------- DRL dataset


@extractor("drl_dataset_rows")
def drl_dataset_rows(ctx: Context) -> Iterator[Row]:
    """One row per study x DRL exam (radiography per projection with retakes, CT the study's
    series, fluoroscopy the study)."""
    from ..classify import view_code

    session, secret = ctx.session, ctx.secret
    _, nat_bands = load_bands(session, "ro-drl-age")
    ids = list(session.scalars(select(Study.id).where(*window_conds(ctx))
                               .order_by(Study.study_datetime, Study.id)))
    devices = {d.id: d for d in session.scalars(select(Device))}
    physical = {i: (d.merged_into_id or i) for i, d in devices.items()}
    fac = _code(secret, "facility", ctx.params.get("facility") or "DEFAULT")
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        studies = session.execute(
            select(Study, Patient).outerjoin(Patient, Patient.id == Study.patient_id)
            .where(Study.id.in_(chunk))).all()
        cls = session.execute(
            select(Classification.study_id, Classification.event_id, CodeSystem.key, Code.code)
            .join(Code, Code.id == Classification.code_id).join(CodeSystem)
            .where(Classification.study_id.in_(chunk)))
        study_codes: dict[int, dict[str, str]] = defaultdict(dict)
        event_codes: dict[int, dict[str, str]] = defaultdict(dict)
        for sid, eid, system, code in cls:
            (study_codes[sid] if eid is None else event_codes[eid]).setdefault(system, code)
        events: dict[int, list[Event]] = defaultdict(list)
        for e in session.scalars(select(Event).where(Event.study_id.in_(chunk),
                                                     Event.is_localizer.is_not(True))):
            events[e.study_id].append(e)
        for s, p in sorted(studies, key=lambda x: (x[0].study_datetime or dt.datetime.min,
                                                   x[0].id)):
            sc = study_codes.get(s.id, {})
            groups: dict[tuple, list[Event]] = defaultdict(list)
            for e in events.get(s.id, []):
                if s.modality in EXPOSURE_EXAM:
                    ec = event_codes.get(e.id, {})
                    key = (ec.get(NATIONAL), ec.get(EUROPEAN), ec.get("ro-nom-rx"),
                           view_code(e.view, e.protocol))
                else:  # a CT series of another region than its study has its own codes
                    c = event_codes.get(e.id) or sc
                    key = (c.get(NATIONAL), c.get(EUROPEAN),
                           next((c[k] for k in NOMENCLATORS if k in c), None), None)
                if key[0] or key[1]:
                    groups[key].append(e)
            for (nat, eu, nom, view), evs in groups.items():
                yield _row(ctx, s, p, _dataset_row(ctx, s, p, devices, physical, secret, fac,
                                                   nat_bands, nat, eu, nom, view, evs))
        session.expunge_all()


def _dataset_row(ctx, s, p, devices, physical, secret, fac, nat_bands, nat, eu, nom, view, evs):
    d = devices.get(physical.get(s.device_id)) if s.device_id else None
    band = band_for_age(nat_bands, days=s.age_days) if s.age_days is not None else None

    def total(attr):
        v = [getattr(e, attr) for e in evs if getattr(e, attr) is not None]
        return _r(sum(v)) if v else None

    ctdi = [e.ctdivol for e in evs if e.ctdivol is not None]
    kvp = [e.kvp for e in evs if e.kvp is not None]
    phantoms = {e.ct_phantom_cm for e in evs if e.ct_phantom_cm}
    return {
        "dataset_version": ctx.spec.version, "facility": fac,
        "device": device_code(secret, d),
        "modality": s.modality, "manufacturer": d.manufacturer if d else None,
        "model": d.model_name if d else None,
        "period": s.study_datetime.strftime("%Y-%m") if s.study_datetime else None,
        "patient": patient_code(secret, p),
        "sex": p.sex if p else None, **coarse_age(s.age_days),
        "age_group": band.key if band else None,
        "weight_kg": s.weight_kg,
        "weight_source": s.weight_source.value if s.weight_source else None,
        "weight_reported_kg": s.weight_reported_kg,
        "height_m": s.height_m, "local_exam": s.study_description, "exam_code": nom,
        "drl_exam_national": nat, "drl_exam_european": eu, "projection": view,
        "contrast": s.contrast_mode, "events": len(evs),
        "dlp": total("dlp"), "ctdivol_mean": _r(sum(ctdi) / len(ctdi), 3) if ctdi else None,
        "ctdivol_max": _r(max(ctdi), 3) if ctdi else None,
        "phantom": phantoms.pop() if len(phantoms) == 1 else None,
        "dap": total("dap"), "kar": total("rp_dose"), "fluoro_time": total("fluoro_time_s"),
        "kvp_mean": _r(sum(kvp) / len(kvp), 1) if kvp else None, "mas_total": total("mas"),
    }


__all__ = ["survey_rows", "drl_dataset_rows", "Row"]
