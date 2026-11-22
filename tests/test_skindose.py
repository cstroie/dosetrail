"""Skin dose model: checked against hand calculations of the same geometry."""

from types import SimpleNamespace as E

import pytest

from dosimetrix import skindose as sd

ADULT = 40 * 366  # days


def ev(**kw):
    base = dict(rp_dose=1.0, dap=100.0, sid_mm=1100.0, dist_source_iso_mm=750.0,
                dist_source_rp_mm=600.0, field_w_mm=None, field_h_mm=None,
                primary_angle=0.0, secondary_angle=0.0, patient_position="headfirst supine")
    base.update(kw)
    return E(**base)


def test_pa_beam_peak_matches_inverse_square():
    r = sd.compute([ev()], ADULT)
    b = sd.phantom(ADULT, None).b  # posterior skin at y = -b, 75 - b cm from the source
    expected = 1000 * (60 / (75 - b)) ** 2 * sd.BSF * sd.F_TISSUE * sd.T_TABLE
    assert r["psd_mgy"] == pytest.approx(expected, rel=0.01)
    assert r["psd_at"] == {"theta": 180, "z_cm": 0.0}  # posterior midline
    assert r["estimate"] is True and r["events_used"] == 1


def test_field_size_from_dap_over_kar():
    r = sd.compute([ev()], ADULT)  # 100 cm2 at the reference point: 10 x 10 cm at 60 cm
    col = r["theta"].index(180)
    exposed = [z for z, row in zip(r["z_cm"], r["dose_mgy"], strict=True) if row[col] > 0]
    b = sd.phantom(ADULT, None).b
    width = 10 * (75 - b) / 60  # the field grows with distance from the source
    assert max(exposed) - min(exposed) == pytest.approx(width, abs=sd.Z_STEP + 0.01)


def test_lateral_and_cranial_angles():
    lao = sd.compute([ev(primary_angle=90.0)], ADULT)
    assert lao["psd_at"]["theta"] == 270  # LAO 90: detector left, beam enters the right
    a = sd.phantom(ADULT, None).a
    no_table = 1000 * (60 / (75 - a)) ** 2 * sd.BSF * sd.F_TISSUE  # not through the table
    assert lao["psd_mgy"] == pytest.approx(no_table, rel=0.02)
    cra = sd.compute([ev(secondary_angle=30.0)], ADULT)
    assert cra["psd_at"]["z_cm"] < 0  # cranial tilt: entrance moves toward the feet


def test_pooling_children_and_missing_values():
    one = sd.compute([ev(rp_dose=0.5)], 5 * 365)
    two = sd.compute([ev(rp_dose=0.25), ev(rp_dose=0.25), ev(rp_dose=None)], 5 * 365)
    assert two["psd_mgy"] == pytest.approx(one["psd_mgy"]) and two["geometries"] == 1
    assert two["events_skipped"] == 1
    child, adult = sd.phantom(5 * 365, None), sd.phantom(ADULT, None)
    assert child.b < adult.b and "5.0 y" in child.note
    assert sd.phantom(5 * 365, 30.0).a > child.a  # heavier than the reference child
    bare = sd.compute([ev(primary_angle=None, secondary_angle=None, dist_source_iso_mm=None,
                          dist_source_rp_mm=None, dap=None)], ADULT)
    notes = " ".join(bare["assumptions"]["notes"])
    assert "posterior–anterior" in notes and "15 × 15" in notes and "isocentre" in notes
    assert sd.compute([], ADULT)["psd_mgy"] is None


def test_head_phantom_for_neuro_procedures():
    head = sd.phantom(14 * 365, 55.0, "head")
    trunk = sd.phantom(14 * 365, 55.0)
    assert head.b < trunk.b and head.note.startswith("head")
    # a smaller head is farther from the source than a trunk's skin: lower entrance dose
    assert sd.compute([ev()], 14 * 365, region="head")["psd_mgy"] < \
        sd.compute([ev()], 14 * 365)["psd_mgy"]
    assert sd.HEAD_PATTERN.search("dsa neuro") and not sd.HEAD_PATTERN.search("dsa body")
