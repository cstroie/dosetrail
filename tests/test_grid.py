"""Second patient-group grid (ICRP 135 / RP 185, extended): resolver, stats, report."""

import datetime as dt

import pytest

from dosimetrix import classify, stats
from dosimetrix.db.models import Device, Event, Patient, Study
from dosimetrix.grid import Grid
from dosimetrix.reports import grid as grid_report
from dosimetrix.seeds.loader import load_all

T = dt.datetime(2026, 5, 4, 9, 0)


def test_parameters_and_bands(session):
    load_all(session)
    g = Grid(session)
    p = g.parameter
    assert p("ro-nom-ct", "CT-CR") == "age"  # RP 185 head exam via the crosswalk
    assert p("ro-nom-ct", "CT-TH") == "weight"
    assert p("ro-nom-rx", "RG-TH") == "weight"
    assert p("ro-nom-rx", "RG-CR") == "age"
    assert p("ro-drl-ms838-2025", "CT-CAP") == "age"  # national codes inherit
    assert p("ro-drl-ms838-2025", "RX-TORACE-PA-AP") == "weight"
    assert p("ro-nom-rx", None) is None
    head, body = ("ro-nom-ct", "CT-CR"), ("ro-nom-ct", "CT-TH")
    assert g.group(*head, 0, None) == "age:0-3mo"
    assert g.group(*head, 120, None) == "age:3mo-1y"
    assert g.group(*head, int(6 * 365.25) + 1, None) == "age:6-18y"
    assert g.group(*head, None, 20.0) == "age:?"
    assert g.group(*body, 100, 4.99) == "weight:0-5kg"
    assert g.group(*body, 100, 5.0) == "weight:5-15kg"  # exactly at a limit: upper group
    assert g.group(*body, 100, 110.0) == "weight:110kg+"
    assert g.group(*body, 100, None) == "weight:?"
    assert g.group("ro-nom-ct", None, 100, 5.0) == "?"
    assert g.labels["weight:?"] == "Weight unknown"
    assert g.ordered_labels()[0] == "0–<3 months"


@pytest.fixture
def studies(session):
    load_all(session)
    dev = Device(display_name="CT A", identity_key="g-ct", is_local=True)
    kid = Patient(pseudo_id="P-G1", sex="F")
    session.add_all([dev, kid])
    session.flush()
    rows = [  # (description, age days, weight, DLP)
        ("Head^CAP_SPIRAL_NATIV (Child)", 200, None, 300.0),
        ("Head^CAP_SPIRAL_NATIV (Child)", 50, None, 200.0),
        ("Thorax^TORACE_NATIV (Child)", 2000, 18.0, 80.0),
        ("Thorax^TORACE_NATIV (Child)", 2100, None, 90.0),
    ]
    for i, (desc, age, w, dlp) in enumerate(rows):
        s = Study(study_uid=f"g.{i}", modality="CT", device_id=dev.id, patient_id=kid.id,
                  study_datetime=T, study_description=desc, source_kind="test",
                  age_days=age, weight_kg=w, total_dlp=dlp)
        s.events = [Event(protocol="Topogram", is_localizer=True, dlp=1.0),
                    Event(protocol=desc.split("^")[0], ctdivol=dlp / 20, dlp=dlp,
                          ct_phantom_cm=16 if "Head" in desc else 32)]
        session.add(s)
    session.flush()
    classify.run(session)
    return dev


def test_stats_dimension(session, studies):
    out = stats.compute(session, [Study.modality == "CT"], "dlp", ["exam", "ref_group"],
                        system="ro-nom-ct")
    got = {(x["key"]["exam"], x["key"]["ref_group"]): x["n"] for x in out["groups"]}
    assert got[("CT-CR", "age:3mo-1y")] == 1 and got[("CT-CR", "age:0-3mo")] == 1
    assert got[("CT-TH", "weight:15-30kg")] == 1 and got[("CT-TH", "weight:?")] == 1
    assert out["orders"]["ref_group"][-2:] == ["Weight unknown", "Exam not classified"]
    # sorted by the grid order: age groups first, then weight groups
    keys = [x["key"]["ref_group"] for x in out["groups"]]
    assert keys.index("age:0-3mo") < keys.index("weight:15-30kg")


def test_report(session, studies):
    r = grid_report.build(session, [], T - dt.timedelta(days=1), T + dt.timedelta(days=1),
                          min_n=1)
    rows = {(x["exam"], x["group"], x["quantity"]): x for x in r["rows"]}
    head = rows[("CT-CAP", "age:3mo-1y", "dlp")]
    assert head["parameter"] == "age" and head["n"] == 1 and head["edrl"] == 385.0
    assert head["status"] == "below"
    body = rows[("CT-TORACE", "weight:15-30kg", "dlp")]
    assert body["edrl"] is not None and body["median"] == 80.0
    assert ("CT-TORACE", "weight:?", "dlp") in rows
    assert r["notes"]["body_weight_unknown"] >= 1
    import io

    buf = io.BytesIO()
    grid_report.write_xlsx(r, buf, {"Generat": "test"})
    assert buf.getvalue()[:2] == b"PK"


def test_rp185_head_groups_stop_at_18(session):
    """RP 185 '≥6 y' is paediatric: adults are never compared with its head EDRLs."""
    from dosimetrix.reference import CTReference
    from dosimetrix.stats import BandLookup

    load_all(session)
    head = BandLookup(session, "rp185-head-age", "age")
    assert head(int(17 * 365.25)) == "6y+" and head(int(25 * 365.25)) is None
    ref = CTReference(session)
    codes = {"ec-rp185": "CT-HEAD"}
    assert ref.for_series(codes, int(10 * 365.25), None)["dlp"] == 650.0
    assert ref.for_series(codes, int(25 * 365.25), None)["dlp"] is None
    assert Grid(session).group("ro-nom-ct", "CT-CR", int(25 * 365.25), None) == "age:18y+"


def test_report_without_estimated_weights(session, studies):
    from dosimetrix.db.models import SizeSource

    est = session.query(Study).filter_by(study_uid="g.3").one()
    est.weight_kg, est.weight_source = 18.5, SizeSource.estimated_reference
    session.flush()
    span = (T - dt.timedelta(days=1), T + dt.timedelta(days=1))
    on = grid_report.build(session, [], *span, min_n=1, estimated=True)
    rows = {(x["exam"], x["group"], x["quantity"]): x for x in on["rows"]}
    assert rows[("CT-TORACE", "weight:15-30kg", "dlp")]["n_all"] == 2
    assert rows[("CT-TORACE", "weight:15-30kg", "dlp")]["estimated"] == 1
    off = grid_report.build(session, [], *span, min_n=1, estimated=False)
    rows = {(x["exam"], x["group"], x["quantity"]): x for x in off["rows"]}
    assert rows[("CT-TORACE", "weight:15-30kg", "dlp")]["n_all"] == 1
    assert rows[("CT-TORACE", "weight:?", "dlp")]["n_all"] == 1
    assert off["notes"]["body_weight_estimated"] == 0 and not off["estimated_weights"]
