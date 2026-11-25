"""Radiography protocol names -> anatomy, projection and the size band they state."""

import pytest

from dozimetrix.rxprotocols import INF, band_sort_key, parse


@pytest.mark.parametrize("name,anatomy,proj,kind,lo,hi,label", [
    # the four examples from the request
    ("W035 Torace a.p. 5-15Kg", "Torace", "AP", "weight", 5, 15, "5-15 kg"),
    ("W001a Craniu a.p. 1-6 ani", "Craniu", "AP", "age", 12, 72, "1-6 y"),
    ("W001a Craniu a.p. peste 6 ani", "Craniu", "AP", "age", 72, INF, ">6 y"),
    ("X070 Mana d.v./v.d. 30-50 Kg", "Mana", "DV", "weight", 30, 50, "30-50 kg"),
    # spellings seen on the Ysio
    ("W035 Torace a.p. 10kg-15kg", "Torace", "AP", "weight", 10, 15, "10-15 kg"),
    ("W035 Torace a.p. 2500g-3000g", "Torace", "AP", "weight", 2.5, 3, "2.5-3 kg"),
    ("T035 Torace a.p. <5 Kg", "Torace", "AP", "weight", 0, 5, "<5 kg"),
    ("W080 Abdomen a.p. sub 5 Kg", "Abdomen", "AP", "weight", 0, 5, "<5 kg"),
    ("W035 Grilaj Costal a.p. >50 kg", "Grilaj Costal", "AP", "weight", 50, INF, ">50 kg"),
    ("W033Torace p.a. >70", "Torace", "PA", "weight", 70, INF, ">70 kg"),
    ("W035 Torace lat. peste110Kg", "Torace", "LAT", "weight", 110, INF, ">110 kg"),
    ("W035 Torace a.p.30-50 Kg", "Torace", "AP", "weight", 30, 50, "30-50 kg"),
    ("X096a Femur lat. 15-30", "Femur", "LAT", "weight", 15, 30, "15-30 kg"),
    ("W008 Sinusuri 11-15 Years", "Sinusuri", "", "age", 132, 180, "11-15 y"),
    ("T001a Craniu a.p. 3-12 luni", "Craniu", "AP", "age", 3, 12, "3-12 mo"),
    ("W001a Mandibula >15 Years", "Mandibula", "", "age", 180, INF, ">15 y"),
    ("W002 Piramida Nazala lat 6-10 Years", "Piramida Nazala", "LAT", "age", 72, 120, "6-10 y"),
    ("X109a Picior oblic >50 Kg", "Picior", "OBL", "weight", 50, INF, ">50 kg"),
    ("T001a Craniu a.p./p.a.", "Craniu", "AP/PA", "none", None, None, ""),
    ("OW_LongSpine ap >50 Kg", "Long Coloana", "AP", "weight", 50, INF, ">50 kg"),
    ("W Chest ap. c(10-30kg)", "Torace", "AP", "weight", 10, 30, "10-30 kg"),
])
def test_parse(name, anatomy, proj, kind, lo, hi, label):
    r = parse(name)
    assert (r.anatomy, r.projection, r.kind, r.lo, r.hi, r.label) == (anatomy, proj, kind, lo, hi, label)


def test_no_projection_and_no_band():
    r = parse("W008 Sinusuri")
    assert (r.anatomy, r.projection, r.kind, r.label) == ("Sinusuri", "", "none", "")


def test_program_sets_merge_into_one_anatomy_but_keep_their_prefix():
    a, b = parse("W035 Torace a.p. 5-15Kg"), parse("T035 Torace a.p. 5-15 Kg")
    assert (a.anatomy, a.projection, a.label) == (b.anatomy, b.projection, b.label)
    assert (a.prefix, b.prefix) == ("W035", "T035")
    assert parse("OT_Long Leg >50 Kg").prefix == "OT_"


def test_empty_names():
    assert parse(None) is None and parse("   ") is None


def test_bands_sort_by_their_ends_with_the_unstated_last():
    bands = [("weight", 50.0, INF), ("none", None, None), ("weight", 5.0, 15.0), ("weight", 0.0, 5.0)]
    assert [b[1] for b in sorted(bands, key=lambda b: band_sort_key(*b))] == [0.0, 5.0, 50.0, None]
