"""Statistics: quantile method, grouping, exams, bands, exposure level."""

import datetime as dt

import pytest
from sqlalchemy import select

from dosetrail import classify, stats
from dosetrail.db.models import Device, Event, Study
from dosetrail.seeds.loader import load_all

T = dt.datetime(2026, 5, 4, 9, 0)


def test_quantiles_match_type7():
    v = [1.0, 2.0, 3.0, 4.0]
    assert stats.quantile(v, 0.25) == 1.75
    assert stats.quantile(v, 0.5) == 2.5
    assert stats.quantile(v, 0.75) == 3.25
    assert stats.quantile([5.0], 0.75) == 5.0 and stats.quantile([], 0.5) is None
    d = stats.describe([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    assert d["n"] == 8 and d["mean"] == 5.0 and d["median"] == 4.5
    assert d["sd"] == pytest.approx(2.138, abs=1e-3)  # sample SD (n - 1)
    assert stats.histogram([-3, 0, 1, 2, 9, 10, 11], [0, 5, 10]) == [4, 3]  # ends clamp


@pytest.fixture
def data(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="s-ct", is_local=True)
    rx = Device(display_name="Room 1", identity_key="s-rx", is_local=True)
    session.add_all([dev, rx])
    session.flush()
    # CT heads: ages 2 y (two studies) and 7 y (one)
    for i, (age_y, dlp) in enumerate([(2, 200.0), (2, 300.0), (7, 500.0)]):
        s = Study(study_uid=f"st.{i}", modality="CT", device_id=dev.id, study_datetime=T,
                  study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test",
                  age_days=int(age_y * 365.25) + 10, total_dlp=dlp)
        s.events = [Event(protocol="Topogram", is_localizer=True, ctdivol=0.1, dlp=1.0),
                    Event(protocol="Head", ctdivol=dlp / 20, dlp=dlp)]
        session.add(s)
    # one radiography study: chest AP + chest LAT + forearm
    s = Study(study_uid="sx.1", modality="DX", device_id=rx.id, study_datetime=T,
              study_description="- - - - -", source_kind="test", age_days=3 * 365 + 40,
              weight_kg=14.0, total_dap=0.6)
    s.events = [Event(protocol="W035 Torace a.p.", view="antero-posterior", dap=0.01),
                Event(protocol="W035 Torace lat", view="lateral", dap=0.02),
                Event(protocol="ANTEBRAT", view="antero-posterior", dap=0.002)]
    session.add(s)
    session.flush()
    classify.run(session)
    return dev, rx


def test_ct_by_exam_and_age_band(session, data):
    out = stats.compute(session, [Study.modality == "CT"], "dlp_total", ["exam", "age_band"],
                        system="ro-nom-ct")
    g = {(x["key"]["exam"], x["key"]["age_band"]): x for x in out["groups"]}
    assert set(g) == {("CT-CR", "1-4y"), ("CT-CR", "5-9y")}
    assert g[("CT-CR", "1-4y")]["n"] == 2 and g[("CT-CR", "1-4y")]["median"] == 250.0
    assert g[("CT-CR", "1-4y")]["label"]["exam"] == "CT craniocerebral"
    assert out["n"] == 3 and out["groups"][0]["key"]["age_band"] == "1-4y"  # band order


def test_ct_series_level_skips_localisers_and_uses_drl_codes(session, data):
    out = stats.compute(session, [Study.modality == "CT"], "ctdivol", ["exam"],
                        system="ro-drl-ms838-2025")
    (g,) = out["groups"]
    assert g["key"]["exam"] == "CT-CAP" and g["n"] == 3 and g["min"] == 10.0


def test_radiography_exposures_by_drl_exam(session, data):
    out = stats.compute(session, [Study.modality == "DX"], "dap", ["exam", "view"],
                        system="ro-drl-ms838-2025")
    g = {x["key"]["exam"]: x for x in out["groups"]}
    assert g["RX-TORACE-PA-AP"]["n"] == 1 and g["RX-TORACE-PA-AP"]["median"] == 0.01
    assert g["RX-TORACE-LAT"]["key"]["view"] == "LL"
    assert g[None]["n"] == 1  # the forearm has no DRL code

    only = stats.compute(session, [Study.modality == "DX"], "dap", ["weight_band"],
                         system="ro-drl-ms838-2025", exams=["RX-TORACE-PA-AP"])
    assert [(x["key"]["weight_band"], x["n"]) for x in only["groups"]] == [("5-15kg", 1)]


def test_device_month_histogram_and_errors(session, data):
    dev, _ = data
    out = stats.compute(session, [Study.modality == "CT"], "dlp_total", ["device", "month"],
                        bins=4)
    (g,) = out["groups"]
    assert g["key"] == {"device": dev.id, "month": "2026-05"}
    assert g["label"]["device"] == "CT A" and sum(g["hist"]) == 3 and len(out["bins"]) == 5
    with pytest.raises(stats.StatsError):
        stats.compute(session, [], "nope", [])
    with pytest.raises(stats.StatsError):
        stats.compute(session, [], "dap", ["colour"])
    with pytest.raises(stats.StatsError):
        stats.compute(session, [], "dap", ["age_band"], age_scheme="ext-body-weight")
    assert session.scalar(select(Study).filter_by(study_uid="st.0")).total_dlp == 200.0
