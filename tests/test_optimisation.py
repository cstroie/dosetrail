"""Before / after comparison of an optimisation."""

import datetime as dt

import pytest

from dozimetrix import classify, drl
from dozimetrix.db.models import Device, Event, Study
from dozimetrix.reports import optimisation
from dozimetrix.seeds.loader import load_all

CHANGE = dt.date(2026, 6, 1)
AGE_2Y = int(2 * 365.25) + 30


def test_mann_whitney_known_values():
    u, p = optimisation.mann_whitney(list(range(1, 11)), list(range(11, 21)))
    assert u == 0 and p == pytest.approx(1.827e-4, rel=0.01)  # asymptotic, as scipy (exact: 1.1e-5)
    u, p = optimisation.mann_whitney([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6])
    assert u == 18 and p == pytest.approx(1.0)
    assert optimisation.mann_whitney([], [1.0])[1] == 1.0
    assert optimisation.mann_whitney([2.0] * 5, [2.0] * 5)[1] == 1.0  # all ties: no variance
    # the symmetric case
    assert optimisation.mann_whitney([5, 6, 7], [1, 2, 3])[1] == pytest.approx(
        optimisation.mann_whitney([1, 2, 3], [5, 6, 7])[1])


def test_before_after_per_exam_and_group(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="op-a", is_local=True)
    session.add(dev)
    session.flush()

    def head(uid, when, dlp, age=AGE_2Y):
        s = Study(study_uid=uid, modality="CT", device_id=dev.id, study_datetime=when,
                  study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test",
                  age_days=age)
        s.events = [Event(protocol="Head", dlp=dlp, ctdivol=dlp / 20)]
        session.add(s)

    t0 = dt.datetime.combine(CHANGE, dt.time())
    for i in range(14):  # 1-4 y: 1000 → 600 after the change
        head(f"b.{i}", t0 - dt.timedelta(days=3 + 5 * i), 1000.0 + 10 * (i % 4))
        head(f"a.{i}", t0 + dt.timedelta(days=3 + 5 * i), 600.0 + 10 * (i % 4))
    for i in range(14):  # 10-14 y: unchanged
        head(f"cb.{i}", t0 - dt.timedelta(days=3 + 5 * i), 900.0 + 5 * (i % 3), age=4500)
        head(f"ca.{i}", t0 + dt.timedelta(days=3 + 5 * i), 900.0 + 5 * (i % 3), age=4500)
    for i in range(3):  # 5-9 y: too few
        head(f"sb.{i}", t0 - dt.timedelta(days=10 + i), 800.0, age=2500)
        head(f"sa.{i}", t0 + dt.timedelta(days=10 + i), 500.0, age=2500)
    session.flush()
    classify.run(session)
    rep = optimisation.build(session, [], CHANGE, days=90, exam="CT-CAP",
                             note="  Reader scores unchanged  ")
    assert rep["image_quality_note"] == "Reader scores unchanged"
    assert rep["before"] == ["2026-03-03", "2026-06-01"]
    got = {r["band"]: r for r in rep["rows"] if r["quantity"] == "dlp"}
    young = got["1–4 ani"]
    assert (young["n_before"], young["n_after"]) == (14, 14)
    assert young["change"] == pytest.approx(-0.4, abs=0.01) and young["p"] < 0.001
    assert young["verdict"] == "lower" and young["ratio_after"] < young["ratio_before"]
    assert got["10–14 ani"]["verdict"] == "no_change"
    assert got["5–9 ani"]["verdict"] == "insufficient"
    assert rep["summary"]["lower"] >= 1
    with pytest.raises(drl.DRLError):
        optimisation.build(session, [], CHANGE, set_key="nope")
    with pytest.raises(drl.DRLError, match="days"):
        optimisation.build(session, [], CHANGE, days=3)
