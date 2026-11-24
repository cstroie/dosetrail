import datetime as dt

import pytest

from dozimetrix.patient_age import (
    Band,
    band_for_age,
    band_for_weight,
    completed_months,
    completed_years,
    format_age,
    parse_dicom_age,
)

D = dt.date

RP185_HEAD = [
    Band("0-3mo", "0–<3 mo", None, None, 3, "mo"),
    Band("3mo-1y", "3 mo–<1 y", 3, "mo", 1, "y"),
    Band("1-6y", "1–<6 y", 1, "y", 6, "y"),
    Band("6y+", "≥6 y", 6, "y", None, None),
]
WEIGHT = [
    Band("0-5kg", "<5", None, None, 5, "kg"),
    Band("5-15kg", "5–<15", 5, "kg", 15, "kg"),
    Band("15-30kg", "15–<30", 15, "kg", 30, "kg"),
]


@pytest.mark.parametrize("birth,on,months", [
    (D(2020, 1, 31), D(2020, 2, 29), 1),   # month-end: 31st -> last day of Feb counts
    (D(2020, 1, 31), D(2020, 2, 28), 0),
    (D(2020, 5, 15), D(2020, 8, 14), 2),
    (D(2020, 5, 15), D(2020, 8, 15), 3),
    # leap-day birth: month-end rule -> birthday reached on 28 Feb in non-leap years
    (D(2016, 2, 29), D(2022, 2, 27), 71),
    (D(2016, 2, 29), D(2022, 2, 28), 72),
])
def test_completed_months(birth, on, months):
    assert completed_months(birth, on) == months


def test_exact_birthday_moves_to_upper_band():
    birth = D(2020, 6, 10)
    assert band_for_age(RP185_HEAD, birth=birth, on=D(2026, 6, 9)).key == "1-6y"
    assert band_for_age(RP185_HEAD, birth=birth, on=D(2026, 6, 10)).key == "6y+"
    assert band_for_age(RP185_HEAD, birth=birth, on=D(2020, 9, 9)).key == "0-3mo"
    assert band_for_age(RP185_HEAD, birth=birth, on=D(2020, 9, 10)).key == "3mo-1y"
    assert completed_years(birth, D(2026, 6, 10)) == 6


def test_band_from_days_only():
    assert band_for_age(RP185_HEAD, days=40).key == "0-3mo"
    assert band_for_age(RP185_HEAD, days=400).key == "1-6y"


@pytest.mark.parametrize("w,key", [(4.99, "0-5kg"), (5.0, "5-15kg"), (14.9, "5-15kg"),
                                   (15.0, "15-30kg"), (30.0, None)])
def test_weight_bands_half_open(w, key):
    b = band_for_weight(WEIGHT, w)
    assert (b.key if b else None) == key


def test_dicom_age_and_format():
    assert parse_dicom_age("005Y") == (5, "Y")
    assert parse_dicom_age("011m") == (11, "M")
    assert parse_dicom_age("5Y") is None
    assert format_age(10) == "10 d"
    assert format_age(400) == "13 mo"
    assert format_age(3000) == "8 y"
