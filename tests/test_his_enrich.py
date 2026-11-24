"""HIS enrichment end to end against the synthetic stub: queue, match, sizes, safety rules."""

import datetime as dt

import pytest

from dozimetrix.db.models import Device, HisLookup, SizeSource, Study, StudyRequest
from dozimetrix.his import enrich, servers
from dozimetrix.his.hippobridge import HisError, HisRequestStub
from dozimetrix.ingest.parse import parse
from dozimetrix.ingest.store import Store
from dozimetrix.nationalid import ro_cnp_check_digit
from tests import dicom_factory as f
from tests import his_stub

SECRET = b"t" * 32
NOW = dt.datetime(2025, 11, 6, 12, 0)  # the factory's exams are on 2025-11-06 at 10:00
GIRL = "618010140001"  # + check digit: a girl born 2018-01-01
GIRL += str(ro_cnp_check_digit(GIRL))


@pytest.fixture(autouse=True)
def exams_count_as_new(monkeypatch):
    """The factory's exams are from 2025: treat them as new imports here."""
    from dozimetrix.ingest import store

    monkeypatch.setattr(store, "HIS_NEW_DAYS", 10 ** 5)


@pytest.fixture
def stub():
    s = his_stub.Stub()
    yield s
    s.close()


@pytest.fixture
def his(session, stub):
    return servers.create(session, SECRET, {"name": "HIS", "base_url": stub.url,
                                            "username": his_stub.USER,
                                            "password": his_stub.PASSWORD,
                                            "min_interval_s": 0})


def _ingest(session, uid, pid=GIRL, weight=None, birth="20180101"):
    r = Store(session, SECRET).store(parse(f.dx_image(
        study_uid=uid, sop_uid=uid + ".1", patient_id=pid, weight=weight, birth=birth)))
    return session.get(Study, r.study_id)


def _run(session, **kw):
    return enrich.run_batch(session, SECRET, now=NOW, sleep=lambda s: None, **kw)


def test_new_studies_are_queued_only_with_a_server_and_policy(session, stub):
    st = _ingest(session, "1.9.1")
    assert session.get(HisLookup, st.id) is None  # no HIS server yet
    servers.create(session, SECRET, {"name": "HIS", "base_url": stub.url})
    st = _ingest(session, "1.9.2")
    assert session.get(HisLookup, st.id).status == "pending"
    session.get(Device, st.device_id).his_policy = "none"
    st = _ingest(session, "1.9.3")
    assert session.get(HisLookup, st.id) is None


def test_old_exams_are_history_and_not_queued(session, stub, his, monkeypatch):
    from dozimetrix.ingest import store

    monkeypatch.setattr(store, "HIS_NEW_DAYS", 7)
    st = _ingest(session, "1.9.5")  # an exam from 2025 arriving now (PACS backfill)
    assert session.get(HisLookup, st.id) is None
    assert st.patient.sex == "F" and st.age_precision.value == "exact"  # CNP still applies


def test_request_on_the_exam_day_gives_weight_height_and_indication(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="30", height="130")
    stub.requests["77"] = [
        his_stub.request("R-old", "2025-10-01T09:00:00", national_id=GIRL, weight="19"),
        his_stub.request("R1", "2025-11-06T09:41:00", national_id=GIRL, weight="21,5",
                         height="118"),
    ]
    st = _ingest(session, "1.9.10")
    stats = _run(session)
    assert stats["matched"] == 1
    lk = session.get(HisLookup, st.id)
    assert lk.request_id == "R1" and lk.match_score > 0.9
    assert (st.weight_kg, st.weight_source) == (21.5, SizeSource.chart_same_day)
    assert (st.height_m, st.height_source) == (1.18, SizeSource.chart_same_day)
    req = session.get(StudyRequest, st.id)
    assert req.indication == "cough, fever" and req.ward == "PEDIATRICS 1"
    assert stats["calls"] == 3  # patient, listing, order form: no login checks


def test_no_request_uses_the_chart_and_retries_later(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22", height="120")
    st = _ingest(session, "1.9.20")
    assert _run(session)["patient_only"] == 1
    lk = session.get(HisLookup, st.id)
    assert lk.status == "pending" and lk.next_try_at == NOW + dt.timedelta(minutes=15)
    assert (st.weight_kg, st.weight_source) == (22.0, SizeSource.his_chart_undated)
    assert _run(session) == {}  # not due yet


def test_undated_chart_does_not_replace_the_header_but_the_request_does(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="25")
    st = _ingest(session, "1.9.30", weight=23)
    _run(session)
    assert (st.weight_kg, st.weight_source) == (23.0, SizeSource.dicom_unknown)
    stub.requests["77"] = [his_stub.request("R2", "2025-11-05T15:00:00", national_id=GIRL,
                                            weight="24")]
    session.get(HisLookup, st.id).next_try_at = None
    _run(session)
    assert (st.weight_kg, st.weight_source) == (24.0, SizeSource.chart_72h)
    assert st.weight_reported_kg == 23.0


def test_implausible_values_are_rejected(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="21")
    stub.requests["77"] = [his_stub.request("R3", "2025-11-06T09:00:00", national_id=GIRL,
                                            weight="210", height="40")]
    st = _ingest(session, "1.9.40")
    _run(session)
    lk = session.get(HisLookup, st.id)
    assert st.weight_kg == 21.0 and st.weight_source == SizeSource.his_chart_undated
    assert {r["from"] for r in lk.detail["rejected"]} == {"request"}
    assert st.height_m is None


def test_another_persons_record_is_not_used(session, stub, his):
    # the header says a baby born 2025-10-02; the ID's HIS record is an adult
    stub.patients["P-BABY"] = his_stub.patient("88", "P-BABY", birth="1990-03-15", weight="70")
    st = _ingest(session, "1.9.50", pid="P-BABY", birth="20251002")
    assert _run(session)["no_patient"] == 1
    assert st.weight_kg is None or st.weight_source != SizeSource.his_chart_undated
    assert st.patient.birth_date == dt.date(2025, 10, 2)
    assert "another person" in session.get(HisLookup, st.id).detail["rejected_patient"]


def test_his_fills_demographics_for_a_non_national_id(session, stub, his):
    stub.patients["P-77"] = his_stub.patient("77", "P-77", gender="male", birth="2019-05-04")
    st = _ingest(session, "1.9.60", pid="P-77", birth="")
    _run(session)
    assert (st.patient.sex, st.patient.birth_date) == ("M", dt.date(2019, 5, 4))
    assert st.age_days == (dt.date(2025, 11, 6) - dt.date(2019, 5, 4)).days


def test_patient_policy_skips_requests(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22")
    stub.requests["77"] = [his_stub.request("R4", "2025-11-06T09:00:00", national_id=GIRL)]
    st = _ingest(session, "1.9.70")
    session.get(Device, st.device_id).his_policy = "patient"
    assert _run(session)["patient_only"] == 1
    assert session.get(HisLookup, st.id).next_try_at is None  # nothing more to wait for
    assert session.get(StudyRequest, st.id) is None


def test_bad_login_is_caught_by_the_known_patient_and_dry_run_writes_nothing(session, stub,
                                                                            his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22")
    _ingest(session, "1.9.80")
    _run(session)  # found: the known patient
    later = [_ingest(session, f"1.9.8{i}", pid=GIRL) for i in range(1, 7)]
    servers.update(session, SECRET, his.id, {"password": "wrong"})
    with pytest.raises(HisError):
        _run(session)
    assert servers.check(session, SECRET, his.id)["ok"] is False  # the admin Test says why
    servers.update(session, SECRET, his.id, {"password": his_stub.PASSWORD})
    for lk in session.query(HisLookup).filter(HisLookup.study_id.in_([s.id for s in later])):
        lk.next_try_at = None
    session.commit()  # the dry run rolls back to here
    assert _run(session, dry_run=True)["patient_only"] >= 1
    assert later[-1].weight_source != SizeSource.his_chart_undated

def test_choose_request():
    when = dt.datetime(2025, 11, 6, 10)
    s = [HisRequestStub("a", dt.datetime(2025, 11, 4, 10)),
         HisRequestStub("b", dt.datetime(2025, 11, 6, 9, 30)),
         HisRequestStub("c", dt.datetime(2025, 11, 6, 10, 30)),
         HisRequestStub("d", dt.datetime(2025, 11, 1, 10))]
    best, score = enrich.choose_request(s, when, 72, 2)
    assert best.id == "b" and score > 0.9
    assert enrich.choose_request(s[3:], when, 72, 2) == (None, None)


def test_a_broken_his_session_stops_the_batch(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22")
    _ingest(session, "1.9.90")
    _run(session)  # the HIS finds this girl: she becomes the known patient
    stub.broken_session = True  # whoami 404 and every search empty, as seen live
    later = [_ingest(session, f"1.9.9{i}", pid=GIRL) for i in range(1, 8)]
    with pytest.raises(HisError) as e:
        _run(session)
    assert e.value.kind == "retry" and "found before" in str(e.value)
    states = [session.get(HisLookup, st.id) for st in later]
    assert sum(lk.attempts == 0 for lk in states) == 2  # the batch stopped after 5
    assert all(lk.status == "pending" for lk in states)  # the 5 are retried later


def test_whoami_404_alone_does_not_stop_the_worker(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22")
    st = _ingest(session, "1.9.89")
    real = stub.httpd.RequestHandlerClass.do_GET

    def flaky(handler):
        if handler.path.startswith("/api/whoami"):
            return handler._send(404, {"message": "User identity not found"})
        return real(handler)

    stub.httpd.RequestHandlerClass.do_GET = flaky
    try:
        assert _run(session)["patient_only"] == 1
    finally:
        stub.httpd.RequestHandlerClass.do_GET = real
    assert session.get(HisLookup, st.id).his_patient_id == "77"


def test_listings_ask_for_the_exam_year_and_in_background(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL)
    stub.requests["77"] = [his_stub.request("R-NY", "2024-12-31T20:00:00", national_id=GIRL)]
    st = _ingest(session, "1.9.88")
    st.study_datetime = dt.datetime(2025, 1, 1, 9)  # New Year: window reaches into 2024
    session.get(HisLookup, st.id).next_try_at = None
    enrich.run_batch(session, SECRET, now=dt.datetime(2025, 1, 1, 12), sleep=lambda s: None)
    assert session.get(HisLookup, st.id).request_id == "R-NY"
    assert sorted(d[:4] for d in stub.dts) == ["2024", "2025"]
    assert set(stub.background) == {"1"}

def test_outputs_study_summary_quality_and_survey(session, stub, his):
    from dozimetrix import quality
    from dozimetrix.api import data
    from dozimetrix.export import survey
    from dozimetrix.seeds.loader import load_all

    load_all(session)
    stub.patients[GIRL] = his_stub.patient("77", GIRL)
    stub.requests["77"] = [his_stub.request("R9", "2025-11-06T09:30:00", national_id=GIRL,
                                            weight="20")]
    st = _ingest(session, "1.9.95")
    lost = _ingest(session, "1.9.96", pid="P-NOBODY")
    for hours in (0, 1, 3, 30):  # three retries, then "not found" is final
        enrich.run_batch(session, SECRET, now=NOW + dt.timedelta(hours=hours),
                         sleep=lambda s: None)
    assert session.get(HisLookup, lost.id).status == "no_patient"
    summary = data.his_summary(session, st.id)
    assert summary["status"] == "matched" and summary["request"]["indication"] == "cough, fever"
    assert "national_id" not in str(summary) and GIRL not in str(summary)
    q = quality.run(session, [Study.id.in_([st.id, lost.id])])
    assert q["rules"]["his_unmatched"]["count"] == 1
    assert q["devices"][0]["share"]["request"] == 0.5
    [row] = [r for r in survey.rows(session, [Study.id == st.id], SECRET)]
    assert row["indication_text"] == "cough, fever" and row["weight_source"] == "chart_same_day"


def test_backfill_queues_history_children_first(session, stub, his):
    old_adult = _ingest(session, "1.9.97", pid="P-AD", birth="19700101")
    old_child = _ingest(session, "1.9.98", pid="P-CH", birth="20200101")
    session.query(HisLookup).delete()
    ids, counts = enrich.backfill_candidates(session, None, None)
    assert set(ids) >= {old_adult.id, old_child.id} and counts["children"] >= 1
    enrich.enqueue(session, ids)
    later = NOW + dt.timedelta(days=30)
    his.backfill_hours = f"{later.hour}-{(later.hour + 1) % 24}"
    first = enrich.due(session, his, later, 1)[0]
    assert first.study_id == old_child.id
    his.backfill_hours = f"{(later.hour + 2) % 24}-{(later.hour + 3) % 24}"
    assert enrich.due(session, his, later, 5) == []  # history waits for its hours


def test_enqueue_adds_missing_resets_on_request_and_repeats_are_harmless(session):
    sids = []
    for i in range(3):
        s = Study(study_uid=f"enq.{i}", source_kind="test", modality="CT")
        session.add(s)
        session.flush()
        sids.append(s.id)
    session.add(HisLookup(study_id=sids[0], status="matched", attempts=2))
    session.flush()
    assert enrich.enqueue(session, [*sids, sids[1]]) == 2  # the known one is left alone
    assert session.get(HisLookup, sids[0]).status == "matched"
    assert session.get(HisLookup, sids[1]).status == "pending"
    assert enrich.enqueue(session, sids, reset=True) == 3
    assert session.get(HisLookup, sids[0]).status == "pending"
    assert session.get(HisLookup, sids[0]).attempts == 0
