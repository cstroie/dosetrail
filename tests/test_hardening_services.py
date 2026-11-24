"""Service hardening: spool names, retention, receiver limits, HIS redirects."""

import os
import time
import urllib.error
import urllib.request

import pytest

from dozimetrix.his.hippobridge import _SafeRedirect
from dozimetrix.net.spool import Spool, SpoolWorker


def test_long_or_odd_uids_do_not_collide(tmp_path):
    sp = Spool(tmp_path)
    base = "1." + "2" * 70
    a = sp.write(base + "1", lambda p: p.write_bytes(b"a"))
    b = sp.write(base + "2", lambda p: p.write_bytes(b"b"))
    assert a != b and a.read_bytes() == b"a"
    assert sp.write("1.2.3", lambda p: p.write_bytes(b"c")).name == "1.2.3.dcm"


def test_old_failed_and_archived_files_are_pruned(tmp_path):
    sp = Spool(tmp_path)
    old = sp.failed / "1.2.dcm"
    old.write_bytes(b"x")
    day = sp.archive / "20200101"
    day.mkdir()
    (day / "1.3.dcm").write_bytes(b"x")
    new = sp.failed / "1.4.dcm"
    new.write_bytes(b"x")
    past = time.time() - 100 * 86400
    for p in (old, day / "1.3.dcm"):
        os.utime(p, (past, past))
    w = SpoolWorker(sp, None, b"k" * 32, retention_days=90)
    assert w.prune() == 2
    assert new.exists() and not old.exists() and not day.exists()
    assert w.prune() == 0  # at most hourly


def _redirect(old, new):
    req = urllib.request.Request(old, headers={"Authorization": "Basic x"})
    return _SafeRedirect().redirect_request(req, None, 302, "Found", {}, new)


def test_his_redirects_stay_on_the_server():
    assert _redirect("https://his.local/a", "https://his.local/b").full_url.endswith("/b")
    with pytest.raises(urllib.error.HTTPError):
        _redirect("https://his.local/a", "https://elsewhere.example/b")
    with pytest.raises(urllib.error.HTTPError):
        _redirect("https://his.local/a", "http://his.local/b")


def test_receiver_refuses_oversized_objects(tmp_path):
    from dozimetrix.net.scp import Receiver

    rx = Receiver(Spool(tmp_path), "DOZ", 0, max_object_mb=0.001, min_free_mb=0)

    class Ev:
        class request:
            AffectedSOPInstanceUID = "1.2.3"

        class assoc:
            class requestor:
                ae_title = "CT1"

        @staticmethod
        def encoded_dataset():
            return b"x" * 5000

    assert rx._on_store(Ev) == 0xA700
    assert not list((tmp_path / "incoming").glob("*.dcm"))
