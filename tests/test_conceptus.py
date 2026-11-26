"""Conceptus dose screening for pregnant patients."""

import datetime as dt

import pytest
from sqlalchemy import select

from dozimetrix import alerts, classify, conceptus
from dozimetrix.api import data
from dozimetrix.db.models import Alert, Device, Event, Patient, Study
from dozimetrix.seeds.loader import load_all

NOW = dt.datetime(2026, 11, 26, 9, 0)


@pytest.fixture
def world(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="cn-a", is_local=True)
    mum = Patient(pseudo_id="P-CN1", sex="F")
    session.add_all([dev, mum])
    session.flush()

    def study(uid, desc, modality="CT", ctdivols=(), **kw):
        s = Study(study_uid=uid, modality=modality, device_id=dev.id, patient_id=mum.id,
                  study_datetime=NOW, study_description=desc, source_kind="test",
                  age_days=30 * 366, pregnant=True, gestation_weeks=10, **kw)
        s.events = [Event(protocol="Topogram", is_localizer=True, ctdivol=0.2)] + [
            Event(protocol="Spiral", ctdivol=c, dlp=c * 40, ct_phantom_cm=32) for c in ctdivols]
        session.add(s)
        session.flush()
        return s

    return study


def test_ranges_and_levels():
    s = Study(modality="CT")
    ev = [Event(ctdivol=12.0), Event(ctdivol=8.0), Event(ctdivol=3.0, is_localizer=True)]
    est = conceptus.estimate(s, ev, "abdomen_pelvis")  # the localizer is left out
    assert est["mgy"] == pytest.approx((11.2, 18.8, 26.0)) and est["level"] == "document"
    assert est["text"].startswith("conceptus dose ≈ 18.8 mGy (11–26; screening")
    big = conceptus.estimate(s, [Event(ctdivol=60.0)], "trunk")
    assert big["level"] == "physicist" and big["text"].endswith("by the physicist")
    tiny = conceptus.estimate(s, [Event(ctdivol=0.5)], "abdomen_pelvis")
    assert tiny["level"] == "negligible"
    head = conceptus.estimate(s, [Event(ctdivol=60.0)], "head")  # scatter only: no number
    assert head["mgy"] is None and head["level"] == "negligible" and "scatter" in head["text"]
    assert conceptus.estimate(s, [], "abdomen_pelvis")["mgy"] is None
    assert conceptus.estimate(s, [Event(ctdivol=5.0)], None)["level"] == "physicist"
    rf = conceptus.estimate(Study(modality="RF"), [], "abdomen_pelvis")
    assert rf["mgy"] is None and "incident" in rf["text"]
    assert conceptus.estimate(Study(modality="DX"), [], None)["text"] == ""


def test_alert_and_study_page_carry_the_estimate(session, world):
    abd = world("cn.1", "Abdomen^ABDOMEN_PELVIS_CT", ctdivols=(12.0, 8.0))
    head = world("cn.2", "Head^CAP_SPIRAL_NATIV (Adult)", ctdivols=(60.0,))
    classify.run(session)
    alerts.evaluate(session, [abd.id, head.id])
    msg = {s.study_uid: a.message for s in (abd, head)
           for a in session.scalars(select(Alert).where(Alert.study_id == s.id))
           if a.message.startswith("Pregnant")}
    assert msg["cn.1"].startswith("Pregnant patient, 10 weeks: CT — conceptus dose ≈ 18.8 mGy")
    assert msg["cn.2"].endswith("head scan, uterus outside the beam, scatter only")
    page = data.study(session, abd.id)["totals"]["conceptus"]
    assert page["mgy"] == [11.2, 18.8, 26.0] and page["level"] == "document"
    session.get(Study, abd.id).pregnant = False
    session.flush()
    assert data.study(session, abd.id)["totals"]["conceptus"] is None
