from sqlalchemy import func, select

from dozimetrix.db.models import Code, CodeSystem, ReferenceSet, ReferenceValue
from dozimetrix.seeds.loader import load_all


def _counts(session):
    return (session.scalar(select(func.count()).select_from(Code)),
            session.scalar(select(func.count()).select_from(ReferenceValue)))


def test_seed_is_idempotent(session):
    load_all(session)
    first = _counts(session)
    load_all(session)
    assert _counts(session) == first
    assert first[0] > 600 and first[1] == 142 + 53


def _value(session, set_key, exam, band, quantity):
    rs = session.scalar(select(ReferenceSet).filter_by(key=set_key))
    for v in rs.values:
        if v.exam_code.code == exam and (v.band.key if v.band else None) == band \
                and v.quantity == quantity:
            return v.value, v.unit, v.applies_to
    raise AssertionError((set_key, exam, band, quantity))


def test_reference_values_spot_checks(session):
    load_all(session)
    # Order MS 838/2025 Table 4 (CT, per scan sequence)
    assert _value(session, "ro-ms838-2025", "CT-CAP", "1-4y", "dlp") == (800, "mGy.cm", "series")
    assert _value(session, "ro-ms838-2025", "CT-TORACE", "10-14y", "dlp")[0] == 460
    # single-value rows belong to the >=15 y column
    assert _value(session, "ro-ms838-2025", "CT-SINUS", "15y+", "dlp")[0] == 350
    assert _value(session, "ro-ms838-2025", "RX-RENOVEZICALA", "10-14y", "dap")[0] == 1.24
    assert _value(session, "ro-ms838-2025", "XA-PTCA", None, "dap") == (60, "Gy.cm2", "study")
    # RP 185 Table 10.2b
    assert _value(session, "ec-rp185-2018", "CT-HEAD", "0-3mo", "dlp")[0] == 300
    assert _value(session, "ec-rp185-2018", "CT-ABDOMEN", "50-80kg", "ctdivol")[0] == 13


def test_national_codes_are_per_section(session):
    load_all(session)
    rx = session.scalar(select(CodeSystem).filter_by(key="ro-insp-1255-rx"))
    xa = session.scalar(select(CodeSystem).filter_by(key="ro-insp-1255-xa"))
    c1_rx = session.scalar(select(Code).filter_by(system_id=rx.id, code="C1"))
    c1_xa = session.scalar(select(Code).filter_by(system_id=xa.id, code="C1"))
    assert c1_rx.meaning.startswith("Craniu")
    assert c1_xa.meaning.startswith("Coronarografie")
