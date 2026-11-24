"""SSDE from Dw: published fits vs the reports' tables; event rules."""

import pytest

from dozimetrix import ssde
from dozimetrix.db.models import Event

# AAPM 204 Table 1D (32 cm) and Table 2D (16 cm): effective diameter -> f
TABLE_32 = {8: 2.76, 10: 2.57, 15: 2.14, 20: 1.78, 25: 1.48, 30: 1.23, 35: 1.02, 45: 0.71}
TABLE_16 = {6: 1.49, 10: 1.27, 15: 1.05, 20: 0.86, 25: 0.71, 30: 0.59, 45: 0.33}


@pytest.mark.parametrize(("phantom", "table"), [(32, TABLE_32), (16, TABLE_16)])
def test_body_fit_matches_the_report_tables(phantom, table):
    for d, f in table.items():
        assert ssde.factor(d, phantom, head=False)[0] == pytest.approx(f, abs=0.01)


def test_ranges_head_and_phantom_rules():
    assert ssde.factor(7.9, 32, head=False) is None  # below the tabulated 8 cm
    assert ssde.factor(46, 16, head=False) is None
    f, method = ssde.factor(15, 16, head=True)  # AAPM 293 Eq. 1
    assert f == pytest.approx(1.9852 * 2.718281828 ** (-0.0486 * 15)) and method == "aapm293_head16"
    assert ssde.factor(15, 32, head=True) is None  # head on the body phantom: no factor


def test_event():
    ev = Event(ctdivol=10.0, wed_mm=200, ct_phantom_cm=32, anatomy="Abdomen", is_localizer=False)
    val, method = ssde.for_event(ev, head=False)
    assert val == pytest.approx(17.8, abs=0.1) and method == "aapm204_body32"
    ev.ct_phantom_cm = 16
    assert ssde.for_event(ev, head=True)[1] == "aapm293_head16"
    ev.wed_mm = None
    assert ssde.for_event(ev, head=True) is None
