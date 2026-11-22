"""Local DRLs: P75 of room medians, single-room median, adoption, review, deletion."""

import datetime as dt
import io

import pytest

from dosetrail import drl, localdrl
from tests.test_drl import data  # noqa: F401 – fixture: CT A (600, 1000), CT B (1300)


def _row(rows, exam, band, quantity):
    (r,) = [r for r in rows if (r["exam"], r["band"], r["quantity"]) == (exam, band, quantity)]
    return r


def test_propose_rooms(session, data):  # noqa: F811
    out = localdrl.propose(session, [], "national", min_n=1)
    ct = _row(out["rows"], "CT-CAP", "1-4y", "dlp")
    # room medians: A 800, B 1300 -> P75 of [800, 1300] = 1175, 3 significant figures
    assert ct["n_rooms"] == 2 and ct["method"] == "p75_rooms"
    assert ct["value"] == pytest.approx(1180) and ct["reference"] == 800
    assert ct["status"] == "above"
    # min_n 2: only room A counts -> its median
    one = _row(localdrl.propose(session, [], "national", min_n=2)["rows"],
               "CT-CAP", "1-4y", "dlp")
    assert one["method"] == "median_room" and one["value"] == 800
    # radiography per study (the retake summed)
    assert _row(out["rows"], "RX-CAP-AP", "1-4y", "dap")["value"] == pytest.approx(0.15)
    icrp = localdrl.propose(session, [], "icrp", min_n=1)
    assert _row(icrp["rows"], "CT-CAP", "1-6y", "dlp")["value"] == pytest.approx(1180)


def test_adopt_review_delete(session, data):  # noqa: F811
    rows = [r for r in localdrl.propose(session, [], "national", min_n=1)["rows"]
            if r["value"] and r["exam"] == "CT-CAP"]
    rs = localdrl.adopt(session, rows, name="Local 2026", grid="national",
                        start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31), min_n=1,
                        query="origin:local", now=dt.datetime(2026, 12, 31, 12))
    assert rs.key.startswith("local-national-") and len(rs.values) == len(rows)
    # usable as any reference set
    cmp = drl.compare(session, [], rs.key, min_n=1)
    assert _row(cmp["rows"], "CT-CAP", "1-4y", "dlp")["reference"] == pytest.approx(1180)
    rev = localdrl.review(session, rs.key, [], min_n=1)
    r = _row(rev["rows"], "CT-CAP", "1-4y", "dlp")
    assert r["local_drl"] == pytest.approx(1180) and r["change_pct"] == 0
    assert [x["device_name"] for x in r["rooms_above"]] == ["CT B"]
    buf = io.BytesIO()
    localdrl.write_review_xlsx(rev, buf, {"Generat": "test"})
    assert buf.getvalue()[:2] == b"PK"
    with pytest.raises(localdrl.LocalDRLError):
        localdrl.delete(session, "ro-ms838-2025")  # bundled sets stay
    assert localdrl.delete(session, rs.key)["values"] == len(rows)
    with pytest.raises(localdrl.LocalDRLError):
        localdrl.adopt(session, [{**rows[0], "value": -1}], name="x", grid="national",
                       start=dt.date(2026, 1, 1), end=dt.date(2026, 2, 1), min_n=1,
                       query="")
