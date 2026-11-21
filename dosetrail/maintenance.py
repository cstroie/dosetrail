"""Maintenance tasks: recompute derived study/device columns from events and studies."""

from __future__ import annotations

from sqlalchemy import and_, bindparam, func, select, update
from sqlalchemy.orm import Session

from .db.models import Device, Event, Patient, Study
from .security import pseudonym


def recompute_ct_columns(session: Session, study_ids: list[int] | None = None) -> int:
    """Per-study CT event counts and max CTDIvol (excluding localizers)."""
    ev = Event.__table__

    def count(kind):
        return (select(func.count()).select_from(ev)
                .where(and_(ev.c.study_id == Study.id, ev.c.event_type == kind))
                .scalar_subquery())

    stmt = update(Study).values(
        n_spiral=count("spiral"),
        n_axial=count("axial"),
        n_localizer=(select(func.count()).select_from(ev)
                     .where(and_(ev.c.study_id == Study.id, ev.c.is_localizer.is_(True)))
                     .scalar_subquery()),
        ctdivol_max=(select(func.max(ev.c.ctdivol))
                     .where(and_(ev.c.study_id == Study.id, ev.c.is_localizer.is_(False)))
                     .scalar_subquery()),
    ).where(Study.modality == "CT")
    if study_ids is not None:
        stmt = stmt.where(Study.id.in_(study_ids))
    return session.execute(stmt, execution_options={"synchronize_session": False}).rowcount


def recompute_device_stats(session: Session) -> int:
    st = Study.__table__
    stmt = update(Device).values(
        n_studies=select(func.count()).select_from(st)
        .where(st.c.device_id == Device.id).scalar_subquery(),
        first_study_at=select(func.min(st.c.study_datetime))
        .where(st.c.device_id == Device.id).scalar_subquery(),
        last_study_at=select(func.max(st.c.study_datetime))
        .where(st.c.device_id == Device.id).scalar_subquery(),
    )
    return session.execute(stmt, execution_options={"synchronize_session": False}).rowcount


def assign_pseudonyms(session: Session, secret: bytes, batch: int = 5000) -> int:
    """Give every patient without one a keyed pseudonym. Collisions raise (unique index)."""
    rows = session.execute(select(Patient.id, Patient.legacy_sha256)
                           .where(Patient.pseudo_id.is_(None))).all()
    stmt = update(Patient.__table__).where(Patient.__table__.c.id == bindparam("pid"))
    params = [{"pid": pid, "pseudo_id": pseudonym(secret, h or f"internal:{pid}")}
              for pid, h in rows]
    for i in range(0, len(params), batch):
        session.connection().execute(stmt, params[i:i + batch])
    return len(params)


def recompute_all(session: Session, secret: bytes | None = None) -> list[str]:
    out = [f"CT studies updated: {recompute_ct_columns(session)}",
           f"devices updated: {recompute_device_stats(session)}"]
    if secret is not None:
        out.append(f"pseudonyms assigned: {assign_pseudonyms(session, secret)}")
    return out
