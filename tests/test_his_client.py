"""HIS: sealed passwords, server configuration and the HippoBridge client (synthetic stub)."""

import datetime as dt

import pytest

from dosimetrix import secretbox
from dosimetrix.his import hippobridge as hb
from dosimetrix.his import servers
from tests import his_stub

SECRET = b"s" * 32


@pytest.fixture
def stub():
    s = his_stub.Stub()
    yield s
    s.close()


def test_secretbox_roundtrip_and_wrong_key():
    token = secretbox.seal(SECRET, "pa55")
    assert token != "pa55" and secretbox.open_(SECRET, token) == "pa55"
    with pytest.raises(secretbox.SecretError):
        secretbox.open_(b"x" * 32, token)


def test_value_parsing():
    assert hb.number("20,5 kg") == 20.5 and hb.number("0") is None and hb.number("") is None
    assert hb.height_m("100") == 1.0 and hb.height_m("1.65") == 1.65
    assert hb.sex("female") == "F" and hb.sex("Masculin") == "M" and hb.sex("?") is None
    assert hb.date("15.03.2018") == dt.date(2018, 3, 15)
    assert hb.yes_no("Nu") is False


def test_server_config_password_is_write_only(session):
    with pytest.raises(servers.ServerError):
        servers.create(session, SECRET, {"name": "HIS", "base_url": "ftp://x"})
    s = servers.create(session, SECRET, {"name": " HIS ", "base_url": "http://his:8080/",
                                          "username": "u", "password": "p",
                                          "modalities": "dx, ct"})
    d = servers.as_dict(s)
    assert d["password_set"] and "password" not in d and "password_enc" not in d
    assert s.base_url == "http://his:8080" and s.modalities == "DX,CT"
    _, changes = servers.update(session, SECRET, s.id, {"password": "q", "timeout": 30})
    assert changes["password"] == "changed" and "q" not in str(changes)
    _, changes = servers.update(session, SECRET, s.id, {"password": ""})
    assert s.password_enc is None
    with pytest.raises(servers.ServerError):
        servers.update(session, SECRET, s.id, {"backfill_hours": "25-3"})


def test_check_verifies_the_login(session, stub):
    s = servers.create(session, SECRET, {"name": "HIS", "base_url": stub.url,
                                          "username": his_stub.USER, "password": "wrong"})
    assert servers.check(session, SECRET, s.id)["ok"] is False
    servers.update(session, SECRET, s.id, {"password": his_stub.PASSWORD})
    res = servers.check(session, SECRET, s.id)
    assert res["ok"] is True and s.last_check_ok is True


def test_client_reads_patient_and_request(stub):
    nid = "5180101400011"
    stub.patients[nid] = his_stub.patient("777", nid, gender="male", weight="21", height="112")
    stub.requests["777"] = [his_stub.request("R1", "2025-11-06T09:50:00", national_id=nid,
                                             weight="20,5", height="110")]
    c = hb.Client(stub.url, his_stub.USER, his_stub.PASSWORD)
    p = c.find_patient(nid)
    assert (p.id, p.sex, p.weight_kg, p.height_m) == ("777", "M", 21.0, 1.12)
    [stub_r] = c.list_requests("777", "radio")
    assert stub_r.authored_on == dt.datetime(2025, 11, 6, 9, 50) and stub_r.region == "Chest"
    r = c.get_request("R1")
    assert (r.weight_kg, r.height_m, r.ward, r.indication) == (20.5, 1.1, "PEDIATRICS 1",
                                                               "cough, fever")
    assert r.guideline and r.pregnant is False and r.requester == "Dr Test"
    assert c.find_patient("0000000000000") is None


def test_client_errors(stub):
    c = hb.Client(stub.url, his_stub.USER, "wrong")
    with pytest.raises(hb.HisError) as e:
        c.verify_login()
    assert e.value.kind == "auth"
    stub.fail_with = 503
    with pytest.raises(hb.HisError) as e:
        hb.Client(stub.url, his_stub.USER, his_stub.PASSWORD).find_patient("1")
    assert e.value.kind == "retry"
    with pytest.raises(hb.HisError) as e:
        hb.Client("http://127.0.0.1:9", "u", "p", timeout=2).metadata()
    assert e.value.kind == "retry"
