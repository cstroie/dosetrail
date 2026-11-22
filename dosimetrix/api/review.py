"""Exam classification review: coverage, unclassified texts, human decisions."""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, aliased

from .. import classify
from ..db.models import (
    Classification,
    ClassificationStatus,
    Code,
    CodeMapping,
    CodeSystem,
    Device,
    Event,
    Study,
)

FAMILIES = {"rx": ["DX", "CR", "MG", "PX"], "rf": ["RF", "XA"], "ct": ["CT"]}
FIELDS = {"protocol", "anatomy", "study_description", "procedure_name", "requested_procedure"}


class ReviewError(ValueError):
    pass


def _local(q):
    return q.join(Device, Device.id == Study.device_id).where(Device.is_local.is_(True))


def summary(session: Session) -> list[dict]:
    """Per family: local studies, how many have a study-level exam code."""
    out = []
    c = aliased(Classification)
    for fam, mods in FAMILIES.items():
        total = session.scalar(_local(select(func.count()).select_from(Study))
                               .where(Study.modality.in_(mods))) or 0
        done = session.scalar(
            _local(select(func.count(func.distinct(Study.id))).select_from(Study))
            .join(c, and_(c.study_id == Study.id, c.event_id.is_(None)))
            .where(Study.modality.in_(mods))) or 0
        out.append({"family": fam, "modalities": mods, "studies": total, "classified": done,
                    "share": round(done / total, 4) if total else None})
    return out


def unclassified(session: Session, family: str, limit: int = 100) -> list[dict]:
    """Most frequent unclassified texts on local devices.

    Radiography is per exposure (protocol / anatomy / description), CT and fluoroscopy per
    study (description / procedure)."""
    if family not in FAMILIES:
        raise ReviewError(f"unknown family {family!r}")
    mods = FAMILIES[family]
    c = aliased(Classification)
    if family == "rx":
        q = (select(Event.protocol, Event.anatomy, Study.study_description,
                    func.count().label("n"), func.count(func.distinct(Study.id)).label("ns"))
             .join(Study, Study.id == Event.study_id)
             .outerjoin(c, c.event_id == Event.id))
        q = (_local(q).where(Study.modality.in_(mods), c.id.is_(None))
             .group_by(Event.protocol, Event.anatomy, Study.study_description))
        rows = session.execute(q.order_by(func.count().desc()).limit(limit * 3))
        merged: dict[tuple, dict] = {}
        for p, a, d, n, ns in rows:  # group on normalised text: case/spacing variants fold
            key = (classify.normalise(p), classify.normalise(a), classify.normalise(d))
            item = merged.setdefault(key, {"texts": {"protocol": p, "anatomy": a,
                                                     "study_description": d},
                                           "exposures": 0, "studies": 0})
            item["exposures"] += n
            item["studies"] += ns
        items = sorted(merged.values(), key=lambda x: -x["exposures"])[:limit]
    else:
        q = (select(Study.study_description, Study.procedure_name, func.count().label("n"))
             .outerjoin(c, and_(c.study_id == Study.id, c.event_id.is_(None))))
        q = (_local(q).where(Study.modality.in_(mods), c.id.is_(None))
             .group_by(Study.study_description, Study.procedure_name))
        items = [{"texts": {"study_description": d, "procedure_name": p}, "studies": n}
                 for d, p, n in session.execute(q.order_by(func.count().desc()).limit(limit))]
    return items


def codes(session: Session, system: str) -> list[dict]:
    rows = session.execute(
        select(Code.code, Code.meaning, Code.chapter, Code.modality).join(Code.system)
        .where(CodeSystem.key == system).order_by(Code.sort_order, Code.code))
    out = [{"code": c, "meaning": m, "chapter": ch, "modality": mo} for c, m, ch, mo in rows]
    if not out:
        raise ReviewError(f"unknown code system {system!r}")
    return out


def mappings(session: Session) -> list[dict]:
    rows = session.execute(
        select(CodeMapping, Code.code, Code.meaning, CodeSystem.key)
        .join(Code, Code.id == CodeMapping.code_id).join(CodeSystem)
        .where(CodeMapping.status == ClassificationStatus.confirmed)
        .order_by(CodeMapping.modality, CodeMapping.match_field, CodeMapping.match_value))
    return [{"id": m.id, "family": m.modality, "field": m.match_field,
             "text": m.match_value.strip(), "system": sys, "code": code, "meaning": meaning,
             "updated_at": m.updated_at.isoformat(timespec="seconds") if m.updated_at else None}
            for m, code, meaning, sys in rows]


def decide(session: Session, family: str, field: str, text: str, code: str | None) -> dict:
    """Map one text to a nomenclator code (or clear the decision) and reclassify."""
    if family not in FAMILIES:
        raise ReviewError(f"unknown family {family!r}")
    if field not in FIELDS:
        raise ReviewError(f"unknown field {field!r}")
    if not classify.normalise(text):
        raise ReviewError("empty text")
    target = None
    if code:
        system = classify.NOMENCLATOR[family]
        target = session.scalar(select(Code).join(Code.system)
                                .where(CodeSystem.key == system, Code.code == code))
        if target is None:
            raise ReviewError(f"{code!r} is not a code of {system}")
    classify.set_mapping(session, family, field, text, target)
    ids = classify.studies_with_text(session, family, field, text)
    stats = classify.run(session, ids) if ids else {}
    return {"family": family, "field": field, "text": text, "code": code,
            "studies_reclassified": len(ids),
            "classified": stats.get("classified", 0) if stats else 0}
