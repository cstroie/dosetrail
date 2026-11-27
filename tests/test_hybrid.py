"""hybrid.py: PET/CT and SPECT/CT studies."""

from __future__ import annotations

from sqlalchemy import select

from dozimetrix import hybrid
from dozimetrix.db.models import Study


def test_nm_modalities():
    assert hybrid.NM_MODALITIES == ("PT", "NM")


def test_label():
    assert hybrid.label("PT", 500.0) == "PET/CT"
    assert hybrid.label("NM", 300.0) == "SPECT/CT"
    assert hybrid.label("PT", 0.0) == "PET/CT"  # a reported DLP, even 0, is a CT part
    assert hybrid.label("PT", None) == "PT"
    assert hybrid.label("NM", None) == "NM"
    assert hybrid.label("CT", 500.0) == "CT"
    assert hybrid.label("DX", None) == "DX"
    assert hybrid.label(None, None) is None


def test_has_ct_part():
    assert hybrid.has_ct_part("PT", 500.0) is True
    assert hybrid.has_ct_part("NM", 300.0) is True
    assert hybrid.has_ct_part("PT", None) is False
    assert hybrid.has_ct_part("CT", 500.0) is False  # a CT study is not a hybrid
    assert hybrid.has_ct_part("DX", None) is False


def test_matches():
    assert hybrid.matches("PT", 500.0, ["CT"]) is True
    assert hybrid.matches("NM", 200.0, ["CT"]) is True
    assert hybrid.matches("PT", None, ["CT"]) is False
    assert hybrid.matches("NM", None, ["CT"]) is False
    assert hybrid.matches("CT", 500.0, ["CT"]) is True
    assert hybrid.matches("PT", None, ["PT"]) is True
    assert hybrid.matches("PT", 500.0, ["MR"]) is False


def test_modality_in(session):
    for i, (mod, dlp) in enumerate([("CT", 100.0), ("PT", 200.0), ("PT", None), ("MR", None)]):
        session.add(Study(study_uid=f"9.9.{i}", modality=mod, total_dlp=dlp, source_kind="rdsr"))
    session.flush()
    got = session.execute(select(Study.modality, Study.total_dlp)
                          .where(Study.study_uid.like("9.9.%"), hybrid.modality_in(["CT"]))
                          .order_by(Study.study_uid)).all()
    assert [tuple(r) for r in got] == [("CT", 100.0), ("PT", 200.0)]
