"""The server query language must select exactly what the browser's query.js selects."""

import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from dosimetrix.api.query import conditions, parse
from dosimetrix.db.models import Device, Event, Patient, Study, StudyIndication

HERE = Path(__file__).parent
CORPUS = json.loads((HERE / "query_corpus.json").read_text())
EXPECTED = json.loads((HERE / "query_expected.json").read_text())
NOW = dt.datetime.fromisoformat(CORPUS["now"])


@pytest.fixture
def corpus_db(session):
    devs = {}
    for d in CORPUS["devices"]:
        dev = Device(display_name=d["display_name"], model_name=d["model"],
                     station_name=d["station"], is_local=d["origin"] == "local",
                     identity_key=f"corpus-{d['id']}")
        session.add(dev)
        devs[d["id"]] = dev
    session.flush()
    for i, s in enumerate(CORPUS["studies"]):
        p = s["patient"]
        pat = Patient(pseudo_id=p["pseudo_id"], sex=p["sex"], is_test=s["test"])
        session.add(pat)
        session.flush()
        age = p["age"]
        st = Study(
            study_uid=f"9.9.{i}", accession=s["accession"], modality="CT",
            study_datetime=dt.datetime.fromisoformat(s["date"]),
            device_id=devs[s["device_id"]].id, patient_id=pat.id,
            study_description=s["description"],
            age_days=None if age is None else int(age * 365.25 + 100),
            weight_kg=p["weight"], total_dlp=s["dlp_total"], ctdivol_max=s["ctdivol_max"],
            n_spiral=s["events"]["spiral"], n_axial=s["events"]["axial"],
            n_localizer=s["events"]["localizer"], source_kind="test")
        st.events.append(Event(protocol=s["protocol"], event_uid=f"e{i}"))
        session.add(st)
        if s.get("indication"):
            session.flush()
            session.add(StudyIndication(study_id=st.id, code_id=_code(session, s["indication"]),
                                        status="auto"))
    session.flush()
    return session


def _code(session, code: str) -> int:
    from sqlalchemy import select

    from dosimetrix.db.models import Code, CodeSystem

    system = session.scalar(select(CodeSystem).filter_by(key="ro-nom-indications"))
    if system is None:
        system = CodeSystem(key="ro-nom-indications", name="indications")
        session.add(system)
        session.flush()
    c = session.scalar(select(Code).filter_by(system_id=system.id, code=code))
    if c is None:
        c = Code(system_id=system.id, code=code, meaning=code)
        session.add(c)
        session.flush()
    return c.id


@pytest.mark.parametrize("case", [c["q"] for c in CORPUS["cases"]])
def test_parity(corpus_db, case):
    q = (select(Study.accession)
         .outerjoin(Device, Device.id == Study.device_id)
         .outerjoin(Patient, Patient.id == Study.patient_id)
         .where(Study.source_kind == "test", *conditions(parse(case), NOW))
         .order_by(Study.study_uid))
    got = list(corpus_db.scalars(q))
    order = [s["accession"] for s in CORPUS["studies"]]
    assert sorted(got, key=order.index) == EXPECTED[case]


def test_parse_errors_are_reported():
    toks = parse('bogus:1 age:abc date:yesterday "quoted words"')
    assert [t.error is not None for t in toks] == [True, True, True, False]
    assert toks[3].key == "text" and toks[3].value == "quoted words"
