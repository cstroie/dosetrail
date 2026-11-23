"""Maintenance tasks: recompute derived study/device columns from events and studies."""

from __future__ import annotations

from sqlalchemy import and_, bindparam, func, or_, select, update
from sqlalchemy.orm import Session

from .db.models import Device, Event, IngestedObject, Patient, Study
from .hybrid import NM_MODALITIES
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
    ).where(Study.modality.in_(("CT", *NM_MODALITIES)))
    if study_ids is not None:
        stmt = stmt.where(Study.id.in_(study_ids))
    return session.execute(stmt, execution_options={"synchronize_session": False}).rowcount


PET_SOP = {"1.2.840.10008.5.1.4.1.1.128", "1.2.840.10008.5.1.4.1.1.128.1",
           "1.2.840.10008.5.1.4.1.1.130"}
NM_SOP = "1.2.840.10008.5.1.4.1.1.20"
PET_HALF_LIFE_S = 3 * 3600  # F-18 110 min, Ga-68 68 min, C-11, N-13, Rb-82; Tc-99m is 6 h


def relabel_hybrid(session: Session) -> int:
    """A CT study with a radiopharmaceutical administration is a PET/CT or SPECT/CT whose
    CT dose report came first: store it under PT / NM, as if the images had come first.
    PET or NM from the images received, else from the radionuclide's half-life."""
    ids = list(session.scalars(select(Study.id).where(
        Study.modality == "CT",
        select(Event.id).where(Event.study_id == Study.id,
                               Event.event_type == "radiopharmaceutical").exists())))
    for sid in ids:
        sops = set(session.scalars(select(IngestedObject.sop_class_uid).where(
            IngestedObject.study_id == sid).distinct()))
        if sops & PET_SOP:
            mod = "PT"
        elif NM_SOP in sops:
            mod = "NM"
        else:
            hl = [(e.extra_json or {}).get("half_life_s") for e in session.scalars(
                select(Event).where(Event.study_id == sid,
                                    Event.event_type == "radiopharmaceutical"))]
            hl = [h for h in hl if h]
            mod = "PT" if hl and min(hl) < PET_HALF_LIFE_S else "NM"
        session.execute(update(Study).where(Study.id == sid).values(modality=mod),
                        execution_options={"synchronize_session": False})
    return len(ids)


def settle_administrations(session: Session) -> int:
    """One administration seen in two studies of a patient counts once (ingest/store.py)."""
    from .ingest.store import settle_administrations as settle

    rp = select(Event.study_id).where(Event.event_type == "radiopharmaceutical")
    patients = session.scalars(
        select(Study.patient_id).where(Study.id.in_(rp), Study.patient_id.is_not(None))
        .group_by(Study.patient_id).having(func.count() > 1))
    changed: set[int] = set()
    for pid in list(patients):
        for s in session.scalars(select(Study).where(Study.patient_id == pid,
                                                     Study.id.in_(rp))):
            changed.update(settle(session, s))
    return len(changed)


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


def zero_doses_as_missing(session: Session) -> int:
    """Events stored with DAP, Ka,r or SID exactly 0 (the unit reported 0, not a
    measurement): set them to missing, note it in extra_json, recompute study totals."""
    from .ingest.store import ZERO_IS_MISSING, update_study_totals

    cols = [getattr(Event, k) for k in ZERO_IS_MISSING]
    events = list(session.scalars(select(Event).where(or_(*[c == 0 for c in cols]))))
    for e in events:
        zeros = [k for k in ZERO_IS_MISSING if getattr(e, k) == 0]
        for k in zeros:
            setattr(e, k, None)
        e.extra_json = {**(e.extra_json or {}), "reported_zero": zeros}
    session.flush()
    ids = sorted({e.study_id for e in events})
    for sid in ids:
        update_study_totals(session, session.get(Study, sid))
    return len(ids)


def recompute_all(session: Session, secret: bytes | None = None) -> list[str]:
    out = [f"studies with zero doses set to missing: {zero_doses_as_missing(session)}",
           f"PET/CT and SPECT/CT studies filed as CT, relabelled: {relabel_hybrid(session)}",
           f"CT studies updated: {recompute_ct_columns(session)}",
           f"studies sharing an administration with another: "
           f"{settle_administrations(session)}",
           f"devices updated: {recompute_device_stats(session)}"]
    from . import ssde

    out.append(f"CT series with SSDE calculated from Dw: {ssde.update(session)}")
    from . import indications

    out.append(f"clinical indications: {dict(indications.update(session))}")
    if secret is not None:
        out.append(f"pseudonyms assigned: {assign_pseudonyms(session, secret)}")
        from .his.servers import reseal_secrets

        out.append(f"stored secrets resealed with the current key: "
                   f"{reseal_secrets(session, secret)}")
    return out


def forget_studies(session: Session, study_ids: list[int]) -> dict:
    """Remove studies so that query/retrieve fetches them again: their events,
    classifications, alerts and import records go too, and their days lose the
    'retrieved' mark. Used to repair studies imported with a parser bug."""
    from sqlalchemy import delete

    from .db.models import Alert, AppSetting, Classification, IngestedObject
    from .net.qr import DONE_KEY

    ids = sorted(set(study_ids))
    days = {d.date().isoformat() for d in session.scalars(
        select(Study.study_datetime).where(Study.id.in_(ids))) if d}
    out = {"studies": len(ids), "days": sorted(days)}
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        session.execute(delete(Alert).where(Alert.study_id.in_(chunk)))
        session.execute(delete(Classification).where(Classification.study_id.in_(chunk)))
        out["objects"] = out.get("objects", 0) + session.execute(
            delete(IngestedObject).where(IngestedObject.study_id.in_(chunk))).rowcount
        session.execute(delete(Event).where(Event.study_id.in_(chunk)))
        session.execute(delete(Study).where(Study.id.in_(chunk)))
    row = session.get(AppSetting, DONE_KEY)
    if row is not None and row.value:
        row.value = [d for d in row.value if d not in days]
    session.flush()
    recompute_device_stats(session)
    return out
