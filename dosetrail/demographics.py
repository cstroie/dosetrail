"""Patient sex and birth date from several sources, and the study ages that depend on them.

Precedence: a valid national ID (CNP) > the HIS record > what is already stored > a new DICOM
header. Values that disagree with the chosen one are noted in `Patient.demo_conflict` (kept
until a later reconcile finds another conflict) so the Quality page can list them.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import nationalid
from .db.models import AgePrecision, Patient, Study


def _norm_sex(v) -> str | None:
    v = (v or "").strip().upper()[:1]
    return v if v in ("M", "F") else None


FOREIGN_YEARS = 12  # a national/HIS birth date this far from the header's is someone else's


def _real(birth: dt.date | None) -> bool:
    """A header birth date that is not an age-only placeholder (1 January)."""
    return birth is not None and (birth.month, birth.day) != (1, 1)


def _foreign(birth: dt.date | None, headers: list[dt.date]) -> dt.date | None:
    """The header birth date that shows `birth` belongs to another person (e.g. a baby
    registered under a parent's CNP), else None."""
    if birth is None:
        return None
    return next((h for h in headers if abs((h - birth).days) >= FOREIGN_YEARS * 365.25), None)


def reconcile(pat: Patient, *, his_sex=None, his_birth: dt.date | None = None,
              dicom_sex=None, dicom_birth: dt.date | None = None,
              on: dt.date | None = None) -> bool:
    """Apply the precedence rule to `pat`; True when sex or birth date changed."""
    nid = nationalid.parse(pat.patient_id, on)
    stored_src = pat.demo_source or "dicom"
    changed, notes, won = False, [], None
    headers = [b for b in (dicom_birth, pat.birth_date if stored_src == "dicom" else None)
               if _real(b)]
    if nid is not None and (h := _foreign(nid.birth_date, headers)) is not None:
        notes.append(f"cnp birth {nid.birth_date} far from dicom {h}: another person's ID?")
        nid = None
    if (h := _foreign(his_birth, headers)) is not None:
        notes.append(f"his birth {his_birth} far from dicom {h}: another person's record?")
        his_sex = his_birth = None
    for field, cands in (
        ("sex", [("cnp", nid.sex if nid else None), ("his", _norm_sex(his_sex)),
                 (stored_src, _norm_sex(pat.sex)), ("dicom", _norm_sex(dicom_sex))]),
        ("birth", [("cnp", nid.birth_date if nid else None), ("his", his_birth),
                   (stored_src, pat.birth_date), ("dicom", dicom_birth)]),
    ):
        present = [(s, v) for s, v in cands if v is not None]
        if not present:
            if field == "sex" and pat.sex is None and (dicom_sex or "").upper() == "O":
                pat.sex, changed = "O", True
            continue
        src, val = present[0]
        notes += [f"{field} {s}={v} vs {src}={val}" for s, v in present[1:] if v != val]
        attr = "sex" if field == "sex" else "birth_date"
        if getattr(pat, attr) != val:
            setattr(pat, attr, val)
            changed = True
        won = won or src
    if won:
        pat.demo_source = won
    if notes:
        pat.demo_conflict = "; ".join(dict.fromkeys(notes))[:255]
    return changed


def exact_age(study: Study, birth: dt.date | None) -> bool:
    """Set the study's exact age from a birth date; True when it changed."""
    when = study.study_datetime
    if birth is None or when is None or when.date() < birth:
        return False
    days = (when.date() - birth).days
    if study.age_days == days and study.age_precision == AgePrecision.exact:
        return False
    study.age_days, study.age_precision = days, AgePrecision.exact
    return True


def refresh_ages(session: Session, patient_ids) -> list[int]:
    """Recompute exact ages for the patients' studies; the ids of studies that changed."""
    ids = list(patient_ids)
    out: list[int] = []
    for i in range(0, len(ids), 1000):
        rows = session.execute(select(Study, Patient.birth_date).join(Patient)
                               .where(Study.patient_id.in_(ids[i:i + 1000]))).all()
        out += [st.id for st, birth in rows if exact_age(st, birth)]
    session.flush()
    return out


def backfill(session: Session, batch: int = 5000) -> tuple[dict, list[int]]:
    """Apply the precedence rule to every patient with an ID (offline: national IDs only).

    Returns (counts, ids of studies whose age changed). The caller commits or rolls back.
    """
    from collections import Counter

    counts: Counter = Counter()
    changed_studies: list[int] = []
    last = 0
    while True:
        pats = list(session.scalars(select(Patient).where(Patient.id > last,
                                                          Patient.patient_id.is_not(None))
                                    .order_by(Patient.id).limit(batch)))
        if not pats:
            break
        last = pats[-1].id
        changed = []
        for p in pats:
            counts["patients"] += 1
            before = (p.sex, p.birth_date, p.demo_conflict)
            if nationalid.parse(p.patient_id) is not None:
                counts["national_id"] += 1
            if reconcile(p):
                changed.append(p.id)
                counts["sex_set" if before[0] is None and p.sex else
                       "sex_corrected" if before[0] != p.sex else "sex_same"] += 1
                counts["birth_set" if before[1] is None and p.birth_date else
                       "birth_corrected" if before[1] != p.birth_date else "birth_same"] += 1
            if p.demo_conflict != before[2]:
                counts["conflicts_new"] += 1
        session.flush()
        changed_studies += refresh_ages(session, changed)
    counts["studies_age_changed"] = len(changed_studies)
    return {k: v for k, v in counts.items() if not k.endswith("_same")}, changed_studies
