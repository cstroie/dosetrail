"""Alert rules, evaluation, acknowledgement-safe re-evaluation and notifications."""

import datetime as dt

import pytest
from sqlalchemy import select

from dozimetrix import alerts, classify, notify
from dozimetrix.db.models import Alert, AlertRule, Device, Event, Patient, Study
from dozimetrix.seeds.loader import load_all

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
    assert got["fluoro-kar-5gy"].message == "Ka,r total 5500 mGy ≥ 5000 mGy"
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
    grown = Patient(pseudo_id="P-AL2", sex="F")  # another patient: no 48 h repeat-CT alert
    session.add(grown)
    session.flush()
    adult = _study(session, dev, grown, "al.4", NOW, modality="CT", ctdivol_max=55.0,
                   age_days=40 * 366, study_description="Head^HeadRoutine (Adult)")
    session.flush()
    classify.run(session)
    alerts.evaluate(session, [s.id, adult.id])
    got = _alerts(session, s)
    assert set(got) == {"ct-ctdivol-paed-50", "drl-2x"}
    assert got["drl-2x"].message.startswith("CT-CAP: 1700 mGy·cm = 2.1 × DRL 800 mGy·cm")
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


def test_partial_channel_failure_retries_only_the_failed_channel(session, world):
    dev, kid = world
    s = _study(session, dev, kid, "al.8", NOW, modality="XA", total_rp_dose=6.0,
               age_days=4000)
    alerts.evaluate(session, [s.id])
    cfg = notify.NotifySettings(smtp_host="h", email_to=["a@b"], ntfy_url="http://x/t")
    calls = {"email": 0, "ntfy": 0}
    mail_up = False

    def ntfy(c, m):
        calls["ntfy"] += 1

    def email(c, m):
        calls["email"] += 1
        if not mail_up:
            raise OSError("smtp down")

    senders = {"email": email, "ntfy": ntfy}
    notify.dispatch(session, cfg, NOW, senders)
    a = _alerts(session, s)["fluoro-kar-5gy"]
    assert a.notified_at is None and a.notified_channels == "ntfy"
    assert a.notify_error.startswith("failed (try 1)")
    mail_up = True
    notify.dispatch(session, cfg, NOW, senders)
    assert a.notified_at is not None and a.notify_error is None
    assert a.notified_channels == "email,ntfy"
    assert calls == {"email": 2, "ntfy": 1}  # ntfy was not sent a second time


def test_no_channel_keeps_recent_alerts_until_one_is_set_up(session, world):
    dev, kid = world
    recent = _study(session, dev, kid, "al.9", NOW - dt.timedelta(hours=1), modality="XA",
                    total_rp_dose=6.0, age_days=4000)
    old = _study(session, dev, kid, "al.10", NOW - dt.timedelta(days=9), modality="XA",
                 total_rp_dose=6.0, age_days=4000)
    alerts.evaluate(session, [recent.id, old.id])
    stats = notify.dispatch(session, notify.NotifySettings(), NOW)
    assert stats["no_channel"] >= 1 and stats["historical"] >= 1
    a = _alerts(session, recent)["fluoro-kar-5gy"]
    assert a.notified_at is None  # kept: a channel set up later still sends it
    assert _alerts(session, old)["fluoro-kar-5gy"].notify_error == "not sent (historical)"
    sent = []
    notify.dispatch(session, notify.NotifySettings(ntfy_url="http://x/t"), NOW,
                    {"ntfy": lambda c, m: sent.append(m)})
    assert a.notified_at is not None and len(sent) >= 1


def test_ui_settings_override_toml_and_secrets_are_sealed(session):
    from dozimetrix import secretbox
    from dozimetrix.db.models import AppSetting

    key = b"k" * 32
    toml = {"base_url": "https://old.example", "ntfy_url": "http://toml/t", "smtp_port": 25}
    assert notify.load_settings(session, toml, key).channels == ["ntfy"]
    session.add(AppSetting(key=notify.SETTINGS_KEY, value={
        "smtp_host": "mail", "email_to": ["a@b.ro"],
        "smtp_password_enc": secretbox.seal(key, "s3cret"), "bogus": "ignored"}))
    session.flush()
    cfg = notify.load_settings(session, toml, key)
    assert cfg.base_url == "https://old.example" and cfg.ntfy_url == "http://toml/t"
    assert cfg.smtp_port == 25 and cfg.channels == ["email", "ntfy"]
    assert cfg.smtp_password == "s3cret" and not hasattr(cfg, "bogus")
    # another site secret: the password cannot be opened, so it is left unset (not crashed)
    assert notify.load_settings(session, toml, b"z" * 32).smtp_password is None
    with pytest.raises(RuntimeError):
        notify.send_test(notify.NotifySettings(), "email")


def test_site_web_address_and_spool_retention(session, tmp_path):
    import os
    import time

    from dozimetrix import siteconf
    from dozimetrix.db.session import make_sessionmaker
    from dozimetrix.net.spool import Spool, SpoolWorker

    assert siteconf.clean({"base_url": " https://dose.example/ "}) == {"base_url": "https://dose.example"}
    for bad in ({"base_url": "ftp://x"}, {"spool_retention_days": -3}):
        with pytest.raises(siteconf.SiteConfError):
            siteconf.clean(bad)
    cfg = notify.load_settings(session, {"base_url": "https://toml"}, b"k" * 32)
    assert cfg.base_url == "https://toml"
    siteconf.save(session, {"base_url": "https://ui", "spool_retention_days": 5})
    session.flush()
    assert notify.load_settings(session, {"base_url": "https://toml"}, b"k" * 32).base_url == "https://ui"
    maker = make_sessionmaker(session.get_bind())
    session.commit()
    assert siteconf.spool_retention(maker, 0) == 5 and siteconf.spool_retention(None, 7) == 7
    # the receiver prunes with the UI's value, not the toml's 0 (keep everything)
    spool = Spool(tmp_path / "spool")
    old, new = spool.failed / "old.dcm", spool.failed / "new.dcm"
    old.write_bytes(b"x"), new.write_bytes(b"x")
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    w = SpoolWorker(spool, maker, b"k" * 32, retention_days=0)
    assert w.prune() == 1 and not old.exists() and new.exists()


def test_per_patient_rules_alert_once_per_window(session, world):
    dev, kid = world
    ids = []
    for i in range(14):  # 14 radiography studies in 14 weeks, evaluated newest first
        s = _study(session, dev, kid, f"rc.{i}", NOW - dt.timedelta(weeks=i), modality="DX",
                   age_days=3000)
        ids.append(s.id)
    for sid in ids:
        alerts.evaluate(session, [sid])
    rule = session.scalar(select(AlertRule).filter_by(key="rx-count-paed-12m"))
    raised = list(session.scalars(select(Alert).filter_by(rule_id=rule.id)))
    assert len(raised) == 1 and raised[0].value >= 10
    # the alert lists every study of the patient in its window, the triggering one flagged
    rows = alerts.window_studies(session, raised[0].id)
    assert len(rows) == raised[0].value and sum(r["current"] for r in rows) == 1
    assert rows[-1]["current"] and [r["time"] for r in rows] == sorted(r["time"] for r in rows)
    assert alerts.window_studies(session, 10**9) is None


def test_ct_dose_rules_and_repeat(session, world):
    dev, kid = world
    first = _study(session, dev, kid, "ct.1", NOW - dt.timedelta(days=200), modality="CT",
                   total_dlp=2600.0, ctdivol_max=20.0, age_days=4000)
    second = _study(session, dev, kid, "ct.2", NOW - dt.timedelta(hours=30), modality="CT",
                    total_dlp=1500.0, ctdivol_max=62.0, age_days=4200)
    third = _study(session, dev, kid, "ct.3", NOW, modality="CT", total_dlp=4100.0,
                   ctdivol_max=30.0, age_days=4201)
    alerts.evaluate(session, [first.id, second.id, third.id])
    assert _alerts(session, first) == {}
    assert set(_alerts(session, second)) == {"ct-ctdivol-paed-60"}  # Σ 4100 < 5000
    got = _alerts(session, third)
    assert set(got) == {"ct-dlp-4000", "ct-dlp-cumulative-paed-12m", "ct-repeat-48h"}
    assert got["ct-dlp-cumulative-paed-12m"].value == pytest.approx(8200.0)
    assert got["ct-repeat-48h"].value == 2
    kinds = {x["rule"]: x["kind"] for x in alerts.listing(session)}
    assert kinds["ct-dlp-4000"] == "DLP" and kinds["ct-repeat-48h"] == "REPEAT"


def test_cumulative_peak_skin_dose(session, world):
    dev, kid = world
    a = _study(session, dev, kid, "ps.1", NOW - dt.timedelta(days=40), modality="XA",
               psd_mgy=1800.0, age_days=5000)
    b = _study(session, dev, kid, "ps.2", NOW, modality="XA", psd_mgy=1500.0, age_days=5040)
    alerts.evaluate(session, [a.id, b.id])
    assert "fluoro-psd-cumulative-90d" not in _alerts(session, a)
    got = _alerts(session, b)["fluoro-psd-cumulative-90d"]
    assert got.severity == "alert" and got.value == pytest.approx(3300.0)
    assert got.message == "Σ 3300 mGy in 2 studies / 90 d ≥ 3000 mGy"


def test_pregnant_patient(session, world):
    dev, _kid = world
    mother = Patient(pseudo_id="P-AL3", sex="F")
    session.add(mother)
    session.flush()
    ct = _study(session, dev, mother, "pg.1", NOW, modality="CT", age_days=30 * 366,
                pregnant=True, gestation_weeks=12)
    rx = _study(session, dev, mother, "pg.2", NOW, modality="DX", age_days=30 * 366,
                pregnant=True)
    unknown = _study(session, dev, mother, "pg.3", NOW - dt.timedelta(days=9), modality="RF",
                     age_days=30 * 366)
    no = _study(session, dev, mother, "pg.4", NOW - dt.timedelta(days=5), modality="CT",
                age_days=30 * 366, pregnant=False)
    alerts.evaluate(session, [ct.id, rx.id, unknown.id, no.id])
    got = _alerts(session, ct)["pregnant"]
    assert got.severity == "alert" and got.message == "Pregnant patient, 12 weeks: CT"
    for s in (rx, unknown, no):  # radiography is out of the default scope; NULL is not True
        assert "pregnant" not in _alerts(session, s)
    row = next(x for x in alerts.listing(session) if x["rule"] == "pregnant")
    assert (row["kind"], row["value"]) == ("PREGNANT", "12 weeks")


def test_silent_device(session, world):
    dev, kid = world
    quiet = Device(display_name="Quiet room", identity_key="al-2", is_local=True)
    session.add(quiet)
    session.flush()
    last = NOW - dt.timedelta(days=5)
    for d in range(30):  # one study a day on the busy device, then nothing for 5 days
        _study(session, dev, kid, f"sl.{d}", last - dt.timedelta(days=d), modality="DX")
    for d in range(3):  # a room that is quiet anyway never counts as silent
        _study(session, quiet, kid, f"sq.{d}", last - dt.timedelta(days=7 * d), modality="DX")
    assert alerts.evaluate_devices(session, NOW - dt.timedelta(days=2)) == {}
    stats = alerts.evaluate_devices(session, NOW)
    assert stats["new"] == 1
    a = session.scalar(select(Alert))
    assert a.value == pytest.approx(5.0) and a.study_datetime == NOW
    assert a.message.startswith("Angio: no studies for 5.0 days")
    assert session.get(Study, a.study_id).study_uid == "sl.0"  # the device's latest study
    # one alert per silent spell: a later run refreshes its value
    assert alerts.evaluate_devices(session, NOW + dt.timedelta(days=1))["updated"] == 1
    row = alerts.listing(session)[0]
    assert (row["kind"], row["value"], row["title"]) == ("SILENT", "6.0 days", "Angio")
    assert alerts.window_studies(session, a.id) == []
    # silent for longer than 90 days: a retired room, not raised
    retired = Device(display_name="Old room", identity_key="al-3", is_local=True)
    session.add(retired)
    session.flush()
    for d in range(30):
        _study(session, retired, kid, f"so.{d}", NOW - dt.timedelta(days=100 + d),
               modality="DX")
    assert alerts.evaluate_devices(session, NOW + dt.timedelta(days=1))["new"] == 0
    # notifications date it when seen, not by the (old) study
    sent = []
    cfg = notify.NotifySettings(ntfy_url="https://ntfy.invalid/x")
    notify.dispatch(session, cfg, NOW + dt.timedelta(days=1, hours=1),
                    {"ntfy": lambda c, m: sent.append(m)})
    assert len(sent) == 1 and "Latest study: DX" in sent[0].body


def test_device_drift(session, world):
    dev, kid = world
    start = NOW - dt.timedelta(days=98)
    n = 0

    def ct(when, dlp):
        nonlocal n
        n += 1
        s = _study(session, dev, kid, f"dr.{n}", when, modality="CT", total_dlp=dlp,
                   age_days=900, study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
        s.events = [Event(protocol="Head", ctdivol=30.0, dlp=dlp)]

    for i in range(40):  # 12 weeks at ~400 mGy·cm, then two weeks at ~800
        ct(start + dt.timedelta(days=2 * i), 380.0 + i % 5 * 10)
    for i in range(12):
        ct(NOW - dt.timedelta(days=13) + dt.timedelta(hours=12 * i), 800.0)
    session.flush()
    classify.run(session)
    assert alerts.evaluate_devices(session, NOW - dt.timedelta(days=7))["new"] == 0  # 1 week
    for i in range(12):
        ct(NOW - dt.timedelta(days=6) + dt.timedelta(hours=12 * i), 820.0)
    session.flush()
    classify.run(session)
    assert alerts.evaluate_devices(session, NOW)["new"] == 1
    a = session.scalar(select(Alert))
    assert a.value == pytest.approx(820 / 400) and a.message.startswith("CT-CAP ")
    assert "· Angio: median 820 mGy·cm this week (12), 2.00× the week before" in a.message
    # not again for the same device, exam and age group within 28 days
    assert alerts.evaluate_devices(session, NOW + dt.timedelta(days=1))["new"] == 0
    assert alerts.listing(session)[0]["kind"] == "DRIFT"


def test_ssde_takes_the_highest_series(session, world):
    dev, kid = world
    s = _study(session, dev, kid, "ss.1", NOW, modality="CT", age_days=4000)
    s.events = [Event(protocol="Topogram", is_localizer=True, ssde=90.0),
                Event(protocol="Abdomen", ssde=30.0),
                Event(protocol="Abdomen late", ssde_calc=55.0)]  # no reported SSDE
    low = _study(session, dev, kid, "ss.2", NOW - dt.timedelta(days=9), modality="CT",
                 age_days=3990)
    low.events = [Event(protocol="Head", ssde=40.0, ssde_calc=70.0)]  # reported wins
    session.flush()
    alerts.evaluate(session, [s.id, low.id])
    got = _alerts(session, s)["ct-ssde-50"]
    assert got.value == 55.0
    assert got.message == "SSDE per series (reported, else calculated) 55 mGy ≥ 50 mGy " \
                          "(Abdomen late)"
    assert "ct-ssde-50" not in _alerts(session, low)


def test_room_above_drl_two_quarters(session, world):
    dev, kid = world
    n = 0

    def quarter(start, dlp, count=20):
        nonlocal n
        for i in range(count):
            n += 1
            s = _study(session, dev, kid, f"su.{n}", start + dt.timedelta(days=3 * i),
                       modality="CT", age_days=900, total_dlp=dlp,
                       study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
            s.events = [Event(protocol="Head", ctdivol=30.0, dlp=dlp)]  # CT-CAP 1-4 y: 800

    quarter(dt.datetime(2026, 4, 1), 1000.0)
    quarter(dt.datetime(2026, 7, 1), 900.0)
    session.flush()
    classify.run(session)
    rule = session.scalar(select(AlertRule).filter_by(key="drl-sustained-2q"))
    rule.threshold = 1.2  # Q2 1.25x, Q3 1.125x: not both
    assert alerts.evaluate_devices(session, NOW, ["drl_sustained"])["new"] == 0
    rule.threshold = 1.0
    assert alerts.evaluate_devices(session, NOW, ["drl_sustained"])["new"] == 1
    a = session.scalar(select(Alert).filter_by(rule_id=rule.id))
    assert a.value == pytest.approx(1.125)
    assert a.message.startswith("2026-Q3 CT-CAP ")
    assert a.message.endswith("· Angio: median 900 mGy·cm = 1.12× DRL 800 (20); "
                              "quarter before 1.25× (20)")
    # once per room, exam, group and quarter
    assert alerts.evaluate_devices(session, NOW + dt.timedelta(days=5))["new"] == 0
    assert alerts.listing(session)[0]["value"] == "1.12× DRL"
    # too few values in a quarter: not compared
    session.delete(a)
    for s in session.scalars(select(Study).where(Study.study_uid.in_(["su.39", "su.40"]))):
        session.delete(s)
    session.flush()
    assert alerts.evaluate_devices(session, NOW, ["drl_sustained"])["new"] == 0


def test_pet_ct_counts_as_ct_exposure(session, world):
    """The CT part of a PET/CT (stored as PT) counts in the patient's CT rules; a PET
    without a CT dose does not."""
    dev, kid = world
    pet = _study(session, dev, kid, "pt.1", NOW - dt.timedelta(hours=20), modality="PT",
                 total_dlp=3000.0, age_days=4200)
    pet_only = _study(session, dev, kid, "pt.2", NOW - dt.timedelta(hours=10), modality="PT",
                      age_days=4200)
    ct = _study(session, dev, kid, "ct.9", NOW, modality="CT", total_dlp=2500.0,
                ctdivol_max=10.0, age_days=4201)
    alerts.evaluate(session, [pet.id, pet_only.id, ct.id])
    got = _alerts(session, ct)
    assert set(got) == {"ct-dlp-cumulative-paed-12m", "ct-repeat-48h"}
    assert got["ct-dlp-cumulative-paed-12m"].value == pytest.approx(5500.0)
    assert got["ct-repeat-48h"].value == 2
    assert {r["id"] for r in alerts.window_studies(session, got["ct-repeat-48h"].id)} \
        == {pet.id, ct.id}


def test_drl_ratio_for_activity_and_mammography(session, world):
    dev, kid = world
    pet = _study(session, dev, kid, "pt.20", NOW, modality="PT", age_days=7 * 365,
                 admin_activity_mbq=400.0, study_description="PET-CT")
    pet.events = [Event(event_type="radiopharmaceutical", protocol="FDG")]
    adult = Patient(pseudo_id="P-AL2", sex="F")
    session.add(adult)
    session.flush()
    mg = _study(session, dev, adult, "mg.20", NOW, modality="MG", age_days=50 * 365)
    mg.events = [Event(view="CC", laterality="L", agd=3.0), Event(view="MLO", laterality="L",
                                                                  agd=3.0),
                 Event(view="CC", laterality="R", agd=1.0)]
    session.flush()
    classify.run(session, [pet.id, mg.id])
    alerts.evaluate(session, [pet.id, mg.id])
    a = _alerts(session, pet)["drl-2x"]  # 400 MBq in a 7-year-old: DRL 150 MBq
    assert a.value == pytest.approx(400.0) and a.message.startswith("PETCT-FDG: 400 MBq")
    b = _alerts(session, mg)["drl-2x"]  # left breast 6 mGy vs 2.65 mGy per breast
    assert b.value == pytest.approx(6.0) and b.message.startswith("MG-UNILATERAL: 6 mGy")


def test_prefetched_windows_match_per_study_queries(session, world, monkeypatch):
    """A chunk evaluated with the prefetched windows raises exactly the alerts that the
    per-study queries do (counts, sums, once-per-window), CT hybrids included."""
    dev, kid = world
    other = Patient(pseudo_id="P-AL2", sex="F")
    session.add(other)
    session.flush()
    ids = []
    for i in range(16):
        for pat, mod in ((kid, "DX"), (other, "CT" if i % 2 else "PT")):
            s = _study(session, dev, pat, f"pf.{pat.id}.{i}", NOW - dt.timedelta(weeks=i),
                       modality=mod, age_days=3000, total_dlp=400.0 if mod != "DX" else None,
                       n_events=2)
            ids.append(s.id)

    def run():
        session.execute(Alert.__table__.delete())
        session.flush()
        stats = alerts.evaluate(session, ids)
        got = {(r.key, a.study_id, round(a.value, 3), a.message) for a, r in session.execute(
            select(Alert, AlertRule).join(AlertRule))}
        return stats, got

    monkeypatch.setattr(alerts, "PREFETCH_MIN", 10**9)
    _, per_study = run()
    monkeypatch.setattr(alerts, "PREFETCH_MIN", 2)
    _, prefetched = run()
    assert per_study and prefetched == per_study


def test_modality_filter_is_the_study_lists(session, world):
    """The alert queue's modality filter: a group (DX = DX and CR), and a device's modality
    correction replaces the stored modality, as in the study list."""
    dev, kid = world
    fixed = Device(display_name="Mislabelled", identity_key="al-2", is_local=True,
                   modality_override="RF")
    session.add(fixed)
    session.flush()
    ct = _study(session, dev, kid, "mf.1", NOW, modality="CT")
    dx = _study(session, dev, kid, "mf.2", NOW, modality="DX")
    cr = _study(session, dev, kid, "mf.3", NOW, modality="CR")
    rf = _study(session, fixed, kid, "mf.4", NOW, modality="DX")  # stored DX, really RF
    ids = {ct.id, dx.id, cr.id, rf.id}

    def hit(v):
        return set(session.scalars(select(Study.id).where(alerts._modality_cond(v)))) & ids

    assert hit("DX") == {dx.id, cr.id}
    assert hit("CR") == {cr.id}
    assert hit("RF") == {rf.id}
    assert hit("CT,RF") == {ct.id, rf.id}
