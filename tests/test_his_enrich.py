"""HIS enrichment end to end against the synthetic stub: queue, match, sizes, safety rules."""

import datetime as dt

import pytest

from dosetrail.db.models import Device, HisLookup, SizeSource, Study, StudyRequest
from dosetrail.his import enrich, servers
from dosetrail.his.hippobridge import HisError, HisRequestStub
from dosetrail.ingest.parse import parse
from dosetrail.ingest.store import Store
from dosetrail.nationalid import ro_cnp_check_digit
from tests import dicom_factory as f
from tests import his_stub

SECRET = b"t" * 32
NOW = dt.datetime(2025, 11, 6, 12, 0)  # the factory's exams are on 2025-11-06 at 10:00
GIRL = "618010140001"  # + check digit: a girl born 2018-01-01
GIRL += str(ro_cnp_check_digit(GIRL))


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
    assert stats["calls"] == 4  # login, patient, listing, order form


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


def test_bad_login_stops_and_dry_run_writes_nothing(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22")
    st = _ingest(session, "1.9.80")
    servers.update(session, SECRET, his.id, {"password": "wrong"})
    with pytest.raises(HisError):
        _run(session)
    servers.update(session, SECRET, his.id, {"password": his_stub.PASSWORD})
    session.commit()  # the dry run rolls back to here
    assert _run(session, dry_run=True)["patient_only"] == 1
    assert st.weight_source != SizeSource.his_chart_undated
    assert session.get(HisLookup, st.id).status == "pending"


def test_choose_request():
    when = dt.datetime(2025, 11, 6, 10)
    s = [HisRequestStub("a", dt.datetime(2025, 11, 4, 10)),
         HisRequestStub("b", dt.datetime(2025, 11, 6, 9, 30)),
         HisRequestStub("c", dt.datetime(2025, 11, 6, 10, 30)),
         HisRequestStub("d", dt.datetime(2025, 11, 1, 10))]
    best, score = enrich.choose_request(s, when, 72, 2)
    assert best.id == "b" and score > 0.9
    assert enrich.choose_request(s[3:], when, 72, 2) == (None, None)


def test_a_broken_his_session_pauses_instead_of_marking_not_found(session, stub, his):
    stub.patients[GIRL] = his_stub.patient("77", GIRL, weight="22")
    st = _ingest(session, "1.9.90")
    stub.broken_session = True
    with pytest.raises(HisError) as e:
        _run(session)
    assert e.value.kind == "retry"
    assert session.get(HisLookup, st.id).status == "pending"
    assert session.get(HisLookup, st.id).attempts == 0
