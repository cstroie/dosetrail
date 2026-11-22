"""Clinical indications: matching, age variants, human decisions, sources, manual codes."""

import pytest

from dosimetrix import indications
from dosimetrix.db.models import Code, CodeSystem, Study, StudyIndication, StudyRequest
from dosimetrix.ingest.parse import parse
from dosimetrix.ingest.store import Store
from dosimetrix.seeds.loader import load_all
from tests import dicom_factory as f

SECRET = b"i" * 32
CHILD, ADULT = 3000, 15000


@pytest.fixture
def m(session):
    load_all(session)
    return indications.Matcher(session)


def _code(m, text, child):
    r = m.match([("t", text)], child)
    return (r.code, r.status) if r else None


def test_matching_and_age_variants(m):
    assert _code(m, "pneumonie", True) == ("RSP-PNM", "auto")
    assert _code(m, "Apendicita acuta", True) == ("PED-APD", "auto")
    # the guideline's adult item says "for children see Paediatrics": a child gets PED-TCE
    guide = "Traumatism cranio-encefalic (pentru copii - vezi capitolul Pediatrie)"
    assert _code(m, guide, True)[0] == "PED-TCE"
    assert _code(m, "Traumatism cranio-encefalic", False)[0] == "NEU-TCE"
    assert _code(m, "tuse cronica", True)[0] == "PED-TUS"
    assert _code(m, "tuse cronica", False)[0] == "RSP-TUS"
    assert _code(m, "cefalee", False) == ("PED-CEF", "suggested")  # child item, adult
    assert _code(m, "tuse", True) is None  # too vague: left for a human decision
    assert _code(m, "infectie respiratorie acuta joasa", True) is None


def _study(session, uid, age=CHILD, **req):
    st = Study(study_uid=uid, modality="DX", source_kind="test", age_days=age)
    session.add(st)
    session.flush()
    if req:
        session.add(StudyRequest(study_id=st.id, **req))
        session.flush()
    return st


def test_update_sources_decisions_and_manual(session, m):
    a = _study(session, "i.1", indication="tuse seaca", guideline="Situatie clinica neincadrabila")
    b = _study(session, "i.2", indication="Ingestie sau inhalatie de corp strain, la copil",
               guideline="Pediatrie / Torace")
    c = _study(session, "i.3")
    c.indication_text, c.indication_source = "Pneumonie", "csv"
    stats = indications.update(session, [a.id, b.id, c.id])
    assert stats["auto"] == 2 and stats["uncoded"] == 1
    got = session.get(StudyIndication, b.id)
    assert got.source == "his_guide" and got.status == "auto"
    assert session.get(StudyIndication, c.id).source == "csv"
    # a human decision for a text codes every study with it
    from sqlalchemy import select

    code = session.scalar(select(Code).where(Code.code == "PED-TUS"))
    indications.set_mapping(session, "TUSE  SEACA", code)
    indications.update(session, indications.studies_with_text(session, "tuse seaca"))
    assert session.get(StudyIndication, a.id).status == "confirmed"
    rev = indications.review(session)
    assert rev["decisions"][0]["code"] == "PED-TUS" and not rev["items"]
    # a study coded by hand is never recomputed
    indications.set_study(session, b.id, code)
    indications.update(session, [b.id])
    assert session.get(StudyIndication, b.id).status == "manual"
    indications.set_study(session, b.id, None)
    assert session.get(StudyIndication, b.id).status == "auto"


def test_dicom_reason_is_read(session, m):
    ds = f.dx_image(study_uid="1.7.1", sop_uid="1.7.1.1")
    ds.ReasonForTheRequestedProcedure = "Pneumonie"
    r = Store(session, SECRET).store(parse(ds))
    st = session.get(Study, r.study_id)
    assert st.indication_text == "Pneumonie" and st.indication_source == "dicom"
    indications.update(session, [st.id])
    assert session.get(StudyIndication, st.id).status == "auto"
    assert session.scalar(CodeSystem.__table__.select().where(
        CodeSystem.key == "ro-nom-indications")) is not None


def test_stats_dimension_and_query(session, m):
    import datetime as dt

    from dosimetrix import stats
    from dosimetrix.api.query import conditions, parse
    from dosimetrix.db.models import Event

    for i, (t, dap) in enumerate([("pneumonie", 0.1), ("pneumonie", 0.3), ("apendicita", 0.5)]):
        st = _study(session, f"s.{i}", indication=t)
        st.events = [Event(dap=dap)]
    session.flush()
    indications.update(session)
    out = stats.compute(session, [Study.modality == "DX"], "dap", ["indication"])
    got = {g["key"]["indication"]: g["n"] for g in out["groups"]}
    assert got == {"RSP-PNM": 2, "PED-APD": 1}
    sel = conditions(parse("ind:RSP-*"), dt.datetime(2026, 1, 1))
    assert len(session.scalars(Study.__table__.select().where(*sel)).all()) == 2
