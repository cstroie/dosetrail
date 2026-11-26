"""Monthly report digests: schedules, content without patient data, once-a-month sending."""

import datetime as dt

import pytest

from dozimetrix import alerts, classify, digests
from dozimetrix.db.models import Device, Event, Patient, Study
from dozimetrix.notify import NotifySettings
from dozimetrix.seeds.loader import load_all

NOW = dt.datetime(2026, 12, 1, 8, 30)  # the first morning: November goes out
AGE_2Y = int(2 * 365.25) + 30


@pytest.fixture
def world(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="dg-a", is_local=True,
                 last_study_at=NOW - dt.timedelta(days=10))
    pat = Patient(pseudo_id="P-DG1", sex="M", patient_id="SECRET-ID", name="SECRET^NAME")
    session.add_all([dev, pat])
    session.flush()
    for i in range(25):  # 25 head CTs in November at 1200 mGy.cm (DRL 1-4 y: 800)
        s = Study(study_uid=f"dg.{i}", modality="CT", device_id=dev.id, patient_id=pat.id,
                  study_datetime=dt.datetime(2026, 11, 3) + dt.timedelta(hours=7 * i),
                  study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test",
                  age_days=AGE_2Y, total_dlp=1200.0)
        s.events = [Event(protocol="Head", dlp=1200.0, ctdivol=60.0)]
        session.add(s)
    session.flush()
    classify.run(session)
    alerts.evaluate(session, [s.id for s in session.query(Study)])
    return dev


def test_save_validates_and_keeps_the_sent_month(session):
    out = digests.save(session, [{"name": "Radiology CT", "to": ["a@example.org"],
                                  "q": "modality:CT"}])
    assert out[0]["id"] == "radiology-ct" and out[0]["enabled"] and out[0]["last_period"] is None
    session.get(digests.AppSetting, digests.KEY).value = [{**out[0], "last_period": "2026-11"}]
    again = digests.save(session, [{"id": "radiology-ct", "name": "CT", "to": ["a@example.org"]}])
    assert again[0]["last_period"] == "2026-11"
    for bad in ([{"name": "", "to": ["a@example.org"]}], [{"name": "x", "to": []}],
                [{"name": "x", "to": ["nope"]}],
                [{"name": "x", "to": ["a@example.org"]}, {"name": "X", "to": ["a@example.org"]}]):
        with pytest.raises(digests.DigestError):
            digests.save(session, bad)


def test_content_has_no_patient_data(session, world):
    start, end = digests.previous_month(NOW)
    assert (start, end) == (dt.datetime(2026, 11, 1), dt.datetime(2026, 12, 1))
    rep = digests.build(session, "", start, end, NOW)
    assert rep["month"] == "2026-11" and rep["studies"] == 25
    top = rep["above"][0]
    assert top["exam"] == "CT-CAP" and top["ratio"] == pytest.approx(1.5) and top["n"] == 25
    assert rep["alerts_total"] >= 1 and rep["quiet"][0]["room"] == "CT A"
    subject, text, page = digests.render("CT department", rep, "https://dose.example.org")
    assert subject == "[Dozimetrix] CT department: 2026-11"
    assert "1.50 × DRL" in text and "CT A" in text and "dose.example.org/alerts.html" in text
    for blob in (text, page, str(rep)):
        assert "SECRET" not in blob


def test_sent_once_a_month_from_seven_and_retried_after_a_failure(session, world):
    digests.save(session, [{"name": "CT", "to": ["a@example.org"]},
                           {"name": "Off", "to": ["b@example.org"], "enabled": False}])
    cfg = NotifySettings(smtp_host="smtp.example.org", email_to=["x@example.org"])
    sent = []

    def mailer(cfg, to, subject, text, page):
        sent.append((to, subject))

    early = NOW.replace(hour=6)
    assert digests.run_due(session, cfg, early, mailer=mailer) == {}  # before 07:00
    assert digests.run_due(session, cfg, NOW, mailer=mailer) == {"ct": "sent"}
    assert sent == [(["a@example.org"], "[Dozimetrix] CT: 2026-11")]
    assert digests.run_due(session, cfg, NOW + dt.timedelta(hours=1), mailer=mailer) == {
        "ct": "skipped"}
    assert len(sent) == 1
    assert digests.load(session)[0]["last_period"] == "2026-11"
    # the next month: a failing mail server leaves it unsent, so it is tried again
    later = dt.datetime(2027, 1, 1, 9, 0)

    def broken(*a):
        raise OSError("connection refused")

    assert digests.run_due(session, cfg, later, mailer=broken)["ct"].startswith("failed")
    assert digests.load(session)[0]["last_period"] == "2026-11"
    assert digests.run_due(session, cfg, later, mailer=mailer) == {"ct": "sent"}
    assert digests.load(session)[0]["last_period"] == "2026-12"
    # no mail server: reported, not sent
    assert "no e-mail server" in digests.run_due(
        session, NotifySettings(), dt.datetime(2027, 2, 1, 9), mailer=mailer)["ct"]
    # force re-sends
    assert digests.run_due(session, cfg, later, force=True, mailer=mailer) == {"ct": "sent"}
