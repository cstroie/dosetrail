"""Hybrid studies: PET/CT and SPECT/CT.

A hybrid study is stored under its nuclear medicine modality (``PT`` or ``NM``), whichever
object arrived first: the CT dose report of a PET/CT does not make it a CT study. It is
shown as "PET/CT" / "SPECT/CT" when it carries a CT dose (DLP).

The CT part still counts as CT exposure for the patient: rules and filters on modality
``CT`` (thresholds, cumulative DLP, repeat CT) also match a hybrid study with a CT dose.
DRLs and the national CT sheets do not: hybrid procedures have their own codes and DRLs.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import and_, or_

from .db.models import Study

NM_MODALITIES = ("PT", "NM")
LABEL = {"PT": "PET/CT", "NM": "SPECT/CT"}


def label(modality: str | None, total_dlp: float | None) -> str | None:
    """'PET/CT' for a PT study with a CT dose, else the modality."""
    if modality in LABEL and total_dlp is not None:
        return LABEL[modality]
    return modality


def has_ct_part(modality: str | None, total_dlp: float | None) -> bool:
    return modality in LABEL and total_dlp is not None


def matches(modality: str | None, total_dlp: float | None, mods: Iterable[str]) -> bool:
    """Does a study fall under a modality list ('CT' includes the CT part of hybrids)?"""
    mods = set(mods)
    return modality in mods or ("CT" in mods and has_ct_part(modality, total_dlp))


def modality_in(mods: Iterable[str]):
    """SQL condition: Study.modality in mods, hybrids with a CT dose under 'CT'."""
    mods = list(mods)
    cond = Study.modality.in_(mods)
    if "CT" in mods:
        cond = or_(cond, and_(Study.modality.in_(NM_MODALITIES), Study.total_dlp.is_not(None)))
    return cond
