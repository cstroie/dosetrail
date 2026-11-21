"""Alert rules, evaluation, acknowledgement-safe re-evaluation and notifications."""

import datetime as dt

import pytest
from sqlalchemy import select

from dosetrail import alerts, classify, notify
from dosetrail.db.models import Alert, AlertRule, Device, Event, Patient, Study
from dosetrail.seeds.loader import load_all

NOW = dt.datetime(2026, 11, 21, 12, 0)


@pytest.fixture
def world(session):
    load_all(session)
    dev = Device(display_name="Angio", identity_key="al-1", is_local=True)
    kid = Patient(pseudo_id="P-AL1", sex="M", patient_id="SECRET-ID", name="SECRET^NAME")
    session.add_all([dev, kid])
    session.flush()
    return dev, kid


def _study(session, dev, pat, uid, when, **kw):
    s = Study(study_uid=uid, device_id=dev.id, patient_id=pat.id, study_datetime=when,
              source_kind="test", **kw)
    session.add(s)
    session.flush()
    return s


def _alerts(session, study):
    return {r.key: a for a, r in session.execute(
        select(Alert, AlertRule).join(AlertRule).where(Alert.study_id == study.id))}


def test_seed_keeps_edited_rules(session, world):
    rule = session.scalar(select(AlertRule).filter_by(key="fluoro-kar-5gy"))
    rule.threshold = 2.0
    session.flush()
    load_all(session)
    assert session.scalar(select(AlertRule).filter_by(key="fluoro-kar-5gy")).threshold == 2.0


def test_threshold_cumulative_and_reevaluation(session, world):
    dev, kid = world
    first = _study(session, dev, kid, "al.1", NOW - dt.timedelta(days=30), modality="XA",
                   total_rp_dose=3.0, total_dap=40.0, age_days=4000)
    second = _study(session, dev, kid, "al.2", NOW, modality="XA", total_rp_dose=5.5,
                    total_dap=60.0, age_days=4030)
    alerts.evaluate(session, [first.id, second.id])
    assert _alerts(session, first) == {}
    got = _alerts(session, second)
    assert set(got) == {"fluoro-kar-5gy", "fluoro-kar-cumulative-90d"}
    assert got["fluoro-kar-5gy"].severity == "alert"
    assert got["fluoro-kar-5gy"].message == "Ka,r total 5.5 Gy ≥ 5 Gy"
    assert got["fluoro-kar-cumulative-90d"].value == pytest.approx(8.5)

    # acknowledged alerts survive re-evaluation unchanged; open ones follow the value
    got["fluoro-kar-5gy"].status = "acknowledged"
    second.total_rp_dose = 6.0
    session.flush()
    stats = alerts.evaluate(session, [second.id])
    assert stats["new"] == 0 and stats["updated"] == 1
    got = _alerts(session, second)
    assert got["fluoro-kar-5gy"].value == 5.5 and got["fluoro-kar-5gy"].status == "acknowledged"
    assert got["fluoro-kar-cumulative-90d"].value == pytest.approx(9.0)


def test_paediatric_ct_and_drl_ratio(session, world):
    dev, kid = world
    s = _study(session, dev, kid, "al.3", NOW, modality="CT", ctdivol_max=55.0,
               age_days=900, study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
    s.events = [Event(protocol="Head", ctdivol=55.0, dlp=1700.0)]  # CT-CAP 1-4 y: 800
    adult = _study(session, dev, kid, "al.4", NOW, modality="CT", ctdivol_max=55.0,
                   age_days=40 * 366, study_description="Head^HeadRoutine (Adult)")
    session.flush()
    classify.run(session)
    alerts.evaluate(session, [s.id, adult.id])
    got = _alerts(session, s)
    assert set(got) == {"ct-ctdivol-paed-50", "drl-2x"}
    assert got["drl-2x"].message.startswith("CT-CAP: 1.7e+03 mGy·cm = 2.1 × DRL")
    assert "ct-ctdivol-paed-50" not in _alerts(session, adult)  # adult: paediatric rule off


def test_listing_and_notifications_without_identifiers(session, world):
    dev, kid = world
    old = _study(session, dev, kid, "al.5", NOW - dt.timedelta(days=10), modality="XA",
                 total_rp_dose=6.0, age_days=4000, study_description="Cardiac")
    new = _study(session, dev, kid, "al.6", NOW - dt.timedelta(hours=2), modality="XA",
                 total_rp_dose=7.0, age_days=4000, study_description="Cardiac")
    alerts.evaluate(session, [old.id, new.id])
    listed = alerts.listing(session)
    assert {x["kind"] for x in listed if x["study_id"] == new.id} == {"KA,R", "CUMUL."}
    kar = next(x for x in listed if x["study_id"] == new.id and x["kind"] == "KA,R")
    assert kar["level"] == "alert" and kar["title"] == "Cardiac · Angio"
    assert listed[0]["study_id"] == new.id  # newest study first

    sent = []
    cfg = notify.NotifySettings(base_url="https://dose.example", ntfy_url="http://x/t")
    stats = notify.dispatch(session, cfg, NOW, {"ntfy": lambda c, m: sent.append(m)})
    assert stats["historical"] >= 1 and stats["sent_ntfy"] >= 1
    body = "\n".join(m.title + m.body for m in sent)
    assert "SECRET" not in body and "P-AL1" not in body
    assert f"https://dose.example/study.html?id={new.id}" in body and sent[0].urgent
    assert notify.dispatch(session, cfg, NOW, {"ntfy": lambda c, m: sent.append(m)}) == {}


def test_notification_failures_retry_then_give_up(session, world):
    dev, kid = world
    s = _study(session, dev, kid, "al.7", NOW, modality="XA", total_rp_dose=6.0,
               age_days=4000)
    alerts.evaluate(session, [s.id])
    cfg = notify.NotifySettings(ntfy_url="http://x/t")

    def boom(c, m):
        raise OSError("down")

    for i in range(notify.MAX_TRIES):
        notify.dispatch(session, cfg, NOW, {"ntfy": boom})
        a = _alerts(session, s)["fluoro-kar-5gy"]
        assert a.notify_error.startswith(f"failed (try {i + 1})")
    assert a.notified_at is not None  # given up after MAX_TRIES
