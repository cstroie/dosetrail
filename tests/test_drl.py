"""DRL comparison: bands, levels (series / exposure / study), units, status, per device."""

import datetime as dt

import pytest

from dosimetrix import classify, drl
from dosimetrix.db.models import Device, Event, Study
from dosimetrix.seeds.loader import load_all

T = dt.datetime(2026, 6, 1, 9, 0)
AGE_2Y = int(2 * 365.25) + 30


@pytest.fixture
def data(session):
    load_all(session)
    a = Device(display_name="CT A", identity_key="d-a", is_local=True)
    b = Device(display_name="CT B", identity_key="d-b", is_local=True)
    rx = Device(display_name="Room 1", identity_key="d-rx", is_local=True)
    session.add_all([a, b, rx])
    session.flush()
    for i, (dev, dlp) in enumerate([(a, 600.0), (a, 1000.0), (b, 1300.0)]):
        s = Study(study_uid=f"d.{i}", modality="CT", device_id=dev.id, study_datetime=T,
                  study_description="Head^CAP_SPIRAL_NATIV (Child)", source_kind="test",
                  age_days=AGE_2Y)
        s.events = [Event(protocol="Topogram", is_localizer=True, dlp=5.0),
                    Event(protocol="Head", dlp=dlp, ctdivol=dlp / 20)]
        session.add(s)
    # skull AP twice in one study (a retake) + one lateral
    s = Study(study_uid="d.x", modality="DX", device_id=rx.id, study_datetime=T,
              study_description="Skull", source_kind="test", age_days=AGE_2Y)
    s.events = [Event(protocol="W001a Craniu a.p.", view="antero-posterior", dap=0.1),
                Event(protocol="W001a Craniu a.p.", view="antero-posterior", dap=0.05),
                Event(protocol="W001a Craniu lat.", view="lateral", dap=0.08)]
    session.add(s)
    session.flush()
    classify.run(session)
    return a, b


def _row(out, exam, band, quantity):
    (r,) = [r for r in out["rows"] if (r["exam"], r["band"], r["quantity"])
            == (exam, band, quantity)]
    return r


def test_national_set_per_series_and_per_study(session, data):
    out = drl.compare(session, [], "ro-ms838-2025", min_n=1)
    ct = _row(out, "CT-CAP", "1-4y", "dlp")  # reference 800 mGy.cm per series
    (g,) = ct["groups"]
    assert g["n"] == 3 and g["median"] == 1000.0  # localisers excluded
    assert g["ratio"] == pytest.approx(1.25) and ct["status"] == "above"
    ap = _row(out, "RX-CAP-AP", "1-4y", "dap")  # "study": the retake is included
    assert ap["groups"][0]["n"] == 1 and ap["groups"][0]["median"] == pytest.approx(0.15)
    assert _row(out, "CT-TORACE", "1-4y", "dlp")["status"] == "no_data"
    assert _row(out, "NM-RENAL-DMSA", "1-4y", "activity")["status"] == "no_data"
    # each row belongs to one modality group: the page shows one table per group
    assert ct["group"] == "CT" and ap["group"] == "DX"
    assert _row(out, "NM-RENAL-DMSA", "1-4y", "activity")["group"] == "NM"


def test_activity_per_study_and_mammography_per_breast(session, data):
    adult = int(40 * 365.25)
    pets = []
    for i, mbq in enumerate([300.0, 420.0]):
        s = Study(study_uid=f"pt.{i}", modality="PT", study_datetime=T, age_days=adult,
                  study_description="PET-CT", source_kind="test", admin_activity_mbq=mbq)
        s.events = [Event(event_type="radiopharmaceutical", protocol="FDG",
                          extra_json={"activity_mbq": mbq}),
                    Event(event_type="spiral", dlp=600.0 + i * 100)]
        pets.append(s)
    # the delayed scan of the second injection: its activity counts on pt.1 only
    late = Study(study_uid="pt.9", modality="PT", study_datetime=T, age_days=adult,
                 study_description="PET-CT", source_kind="test")
    late.events = [Event(event_type="radiopharmaceutical", protocol="FDG",
                         extra_json={"activity_mbq": 420.0, "same_administration_as": 1})]
    mg = Study(study_uid="mg.1", modality="MG", study_datetime=T, age_days=adult,
               source_kind="test")
    mg.events = [Event(view="CC", laterality="R", agd=1.0), Event(view="MLO", laterality="R",
                                                                  agd=1.5),
                 Event(view="CC", laterality="L", agd=1.2), Event(view="MLO", laterality="L",
                                                                  agd=1.4)]
    session.add_all([*pets, late, mg])
    session.flush()
    classify.run(session)
    out = drl.compare(session, [], "ro-ms838-2025", min_n=1)
    (g,) = _row(out, "PETCT-FDG", "15y+", "activity")["groups"]
    assert g["n"] == 2 and g["median"] == pytest.approx(360.0)
    (g,) = _row(out, "PETCT-FDG", "15y+", "dlp")["groups"]
    assert g["n"] == 2
    (g,) = [r for r in out["rows"] if r["exam"] == "MG-UNILATERAL"][0]["groups"]
    assert g["n"] == 2 and sorted([g["min"], g["max"]]) == [pytest.approx(2.5),
                                                             pytest.approx(2.6)]
    # the same studies through the chart metrics of the MG and NM/PT profiles
    from dosimetrix import stats
    from dosimetrix.db.models import Study as S

    agd = stats.compute(session, [S.modality == "MG"], "agd", [])
    assert agd["groups"][0]["n"] == 4
    act = stats.compute(session, [S.modality == "PT"], "activity", [])
    assert act["groups"][0]["n"] == 2 and act["groups"][0]["median"] == pytest.approx(360.0)
    # and through the browser chart rows: MG one row per view, PT one row per study
    from dosimetrix.api import data as api_data

    mg_rows = api_data.group_rows(session, "", T, "MG")
    assert len(mg_rows) == 4 and {r["agd"] for r in mg_rows} == {1.0, 1.5, 1.2, 1.4}
    pt_rows = api_data.group_rows(session, "", T, "PT")
    assert sorted(r["activity"] or 0 for r in pt_rows) == [0, 300.0, 420.0]
    # each PET study row carries the national activity DRL of its exam (dots vs DRL panel)
    assert all(r["drl"].get("activity") for r in pt_rows if r["activity"])
    # the detail page: per-administration rows and per-breast AGD
    d = api_data.study(session, late.id)
    (adm,) = [e for e in d["events"] if e.get("agent")]
    assert adm["agent"] == "FDG" and adm["activity_mbq"] == 420.0
    assert adm["same_administration_as"] == 1
    assert adm["e_msv"] is None  # no coefficient entered: no effective dose invented
    from dosimetrix.db.models import EffectiveDoseCoefficient

    session.add(EffectiveDoseCoefficient(nuclide="", form="FDG", age_to_years=None,
                                         msv_per_mbq=0.019, source="test"))
    session.flush()
    (adm,) = [e for e in api_data.study(session, late.id)["events"] if e.get("agent")]
    assert adm["e_msv"] == pytest.approx(7.98)
    d = api_data.study(session, mg.id)
    assert {"agd_left_mgy", "agd_right_mgy"} <= d["totals"].keys()
    assert sorted(e["agd_mgy"] for e in d["events"]) == [1.0, 1.2, 1.4, 1.5]


def test_european_set_units_bands_and_devices(session, data):
    a, b = data
    out = drl.compare(session, [], "ec-rp185-2018", by_device=True, min_n=2)
    head = _row(out, "CT-HEAD", "1-6y", "dlp")  # 505 mGy.cm
    groups = {g["device_name"]: g for g in head["groups"]}
    assert groups["CT A"]["n"] == 2 and groups["CT A"]["median"] == 800.0
    assert groups["CT A"]["status"] == "well_above"  # 800 > 1.5 x 505
    assert groups["CT B"]["status"] == "insufficient"  # one study < min_n
    assert head["status"] == "well_above"
    lat = _row(out, "RX-HEAD-LAT", "1-6y", "dap")  # per exposure, in mGy.cm2
    assert lat["unit"] == "mGy.cm2" and lat["groups"][0]["median"] == pytest.approx(80.0)
    with pytest.raises(drl.DRLError):
        drl.compare(session, [], "nope")


def test_trunk_series_of_a_head_study_are_not_compared_with_the_head_drl(session, data):
    s = Study(study_uid="d.t", modality="CT", study_datetime=T, source_kind="test",
              study_description="Head^CAP_SPIRAL_NATIV (Child)", age_days=AGE_2Y)
    s.events = [Event(protocol="Head", dlp=700.0), Event(protocol="TAP nativ", dlp=150.0)]
    session.add(s)
    session.flush()
    classify.run(session, [s.id])
    out = drl.compare(session, [Study.id == s.id], "ro-ms838-2025", min_n=1)
    assert _row(out, "CT-CAP", "1-4y", "dlp")["groups"][0]["n"] == 1
    assert _row(out, "CT-TRUNCHI", "1-4y", "dlp")["groups"][0]["median"] == 150.0
