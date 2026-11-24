"""Local DRLs: pooled P75 or median, ICRP room rule, adoption, review, deletion."""

import datetime as dt
import io

import pytest

from dozimetrix import drl, localdrl
from tests.test_drl import data  # noqa: F401 – fixture: CT A (600, 1000), CT B (1300)


def _row(rows, exam, band, quantity):
    (r,) = [r for r in rows if (r["exam"], r["band"], r["quantity"]) == (exam, band, quantity)]
    return r


def test_propose_rooms(session, data):  # noqa: F811
    out = localdrl.propose(session, [], "national", min_n=1, rule="rooms")
    ct = _row(out["rows"], "CT-CAP", "1-4y", "dlp")
    # room medians: A 800, B 1300 -> P75 of [800, 1300] = 1175, 3 significant figures
    assert ct["n_rooms"] == 2 and ct["method"] == "p75_rooms"
    assert ct["value"] == pytest.approx(1180) and ct["reference"] == 800
    assert ct["status"] == "above"
    # min_n 2: only room A counts -> its median
    one = _row(localdrl.propose(session, [], "national", min_n=2, rule="rooms")["rows"],
               "CT-CAP", "1-4y", "dlp")
    assert one["method"] == "median_room" and one["value"] == 800
    # radiography per study (the retake summed)
    assert _row(out["rows"], "RX-CAP-AP", "1-4y", "dap")["value"] == pytest.approx(0.15)
    assert ct["n_phantom32"] == 0
    from dozimetrix.db.models import Event

    ev = session.query(Event).filter(Event.dlp == 1300.0).one()
    ev.ct_phantom_cm = 32
    session.flush()
    again = _row(localdrl.propose(session, [], "national", min_n=1, rule="rooms")["rows"],
                 "CT-CAP", "1-4y", "dlp")
    # left out of the proposal: room B had only that series, so room A alone remains
    assert again["n_phantom32"] == 1 and again["method"] == "median_room"
    assert again["value"] == 800
    ev.ct_phantom_cm = None
    session.flush()
    icrp = localdrl.propose(session, [], "icrp", min_n=1, rule="rooms")
    assert _row(icrp["rows"], "CT-CAP", "1-6y", "dlp")["value"] == pytest.approx(1180)


def test_pooled_rule(session, data):  # noqa: F811
    """P75 of the pooled values from p75_n, the median below, nothing under min_n."""
    def ct(**kw):
        return _row(localdrl.propose(session, [], "national", **kw)["rows"],
                    "CT-CAP", "1-4y", "dlp")
    # three series (600, 1000 in room A; 1300 in room B)
    enough = ct(min_n=1, p75_n=3)
    assert enough["method"] == "p75" and enough["value"] == pytest.approx(1150)
    few = ct(min_n=1, p75_n=4)
    assert few["method"] == "median" and few["value"] == 1000 and few["n"] == 3
    none = ct(min_n=4, p75_n=30)
    assert none["method"] is None and none["value"] is None and none["n"] == 3
    # the p75 threshold never undercuts the minimum
    assert ct(min_n=4, p75_n=2)["value"] is None
    # default is the pooled rule; a room far from the pooled median is flagged
    assert localdrl.propose(session, [], "national", min_n=1)["rule"] == "pooled"
    assert enough["outlier_rooms"] == []
    room = localdrl._room_rule({1: [1.0] * 12, 2: [1.1] * 12, 3: [10.0] * 12}, 10, {})
    assert room["outlier_rooms"] == ["Device 3"]
    with pytest.raises(localdrl.LocalDRLError):
        localdrl.propose(session, [], "national", rule="mean")


def test_adopt_review_delete(session, data):  # noqa: F811
    rows = [r for r in localdrl.propose(session, [], "national", min_n=1,
                                        rule="rooms")["rows"]
            if r["value"] and r["exam"] == "CT-CAP"]
    rs = localdrl.adopt(session, rows, name="Local 2026", grid="national",
                        start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31), min_n=1,
                        query="origin:local", now=dt.datetime(2026, 12, 31, 12), rule="rooms")
    assert rs.key.startswith("local-national-") and len(rs.values) == len(rows)
    # usable as any reference set
    cmp = drl.compare(session, [], rs.key, min_n=1)
    assert _row(cmp["rows"], "CT-CAP", "1-4y", "dlp")["reference"] == pytest.approx(1180)
    rev = localdrl.review(session, rs.key, [], min_n=1)
    assert rev["rule"] == "rooms"  # the rule the set was made with
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


def test_local_only_exams(session, data):  # noqa: F811
    """A nomenclator exam with no national DRL (CT perfusion) still gets a local DRL."""
    from dozimetrix.classify import run
    from dozimetrix.db.models import Device, Event, Study

    dev = session.query(Device).filter_by(display_name="CT A").one()
    for i, dlp in enumerate([2000.0, 2400.0, 2200.0]):
        s = Study(study_uid=f"p.{i}", modality="CT", device_id=dev.id,
                  study_datetime=dt.datetime(2026, 6, 2), source_kind="test",
                  study_description="Perfuzie cerebrala", age_days=int(2 * 365.25) + 30)
        s.events = [Event(protocol="Perfusion", dlp=dlp, ctdivol=dlp / 20)]
        session.add(s)
    session.flush()
    run(session)
    out = localdrl.propose(session, [], "national", min_n=1, p75_n=3)
    row = _row(out["rows"], "CT-CRPF", "1-4y", "dlp")
    assert row["local_only"] and row["system"] == "ro-nom-ct"
    assert row["reference"] is None and row["status"] is None
    assert row["method"] == "p75" and row["value"] == pytest.approx(2300)
    assert not _row(out["rows"], "CT-CAP", "1-4y", "dlp")["local_only"]
    only = localdrl.propose(session, [], "national", min_n=1, scope="national")
    assert not [r for r in only["rows"] if r["local_only"]]
    with pytest.raises(localdrl.LocalDRLError):
        localdrl.propose(session, [], "national", scope="eu")
    # adoptable and reviewable like any row
    rs = localdrl.adopt(session, [row], name="Perfusion", grid="national",
                        start=dt.date(2026, 1, 1), end=dt.date(2026, 12, 31), min_n=1,
                        query="", rule="pooled", p75_n=3)
    rev = localdrl.review(session, rs.key, [], min_n=1)
    (r,) = rev["rows"]
    assert r["local_only"] and r["reference"] is None
    assert r["local_drl"] == pytest.approx(2300) and r["change_pct"] == 0
