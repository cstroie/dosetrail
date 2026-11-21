"""WHO weight-for-age: LMS z-scores with the restricted extension, percentiles, exclusions."""

import datetime as dt

import pytest

from dosetrail import growth
from dosetrail.db.models import Patient, Study

# real WHO coefficients (boys at birth; girls 12 months; boys 10 y / 120 months)
HEAD = "sex\tage\tl\tm\ts\n"
DAYS = HEAD + "".join(f"{r}\n" for r in [
    "1\t0\t0.3487\t3.3464\t0.14602", "1\t1\t0.3127\t3.3174\t0.14693",
    "2\t1\t0.3809\t3.1695\t0.14", "2\t365\t-0.0756\t8.9481\t0.12271"])
MONTHS = HEAD + "".join(f"{r}\n" for r in [
    "1\t120\t-1.4401\t31.2093\t0.17018", "1\t121\t-1.4479\t31.4613\t0.17085",
    "2\t120\t-1.2194\t31.8559\t0.17816", "2\t121\t-1.2205\t32.1355\t0.17883"])


@pytest.fixture
def tables(tmp_path):
    (tmp_path / "weianthro.txt").write_text(DAYS)
    (tmp_path / "wfawho2007.txt").write_text(MONTHS)
    growth._CACHE.clear()
    return growth.GrowthTables.load(tmp_path)


def test_lms_z_and_restricted_extension():
    lms = growth.LMS(0.3487, 3.3464, 0.14602)
    assert lms.z(3.3464) == pytest.approx(0.0)
    assert lms.z(2.5) == pytest.approx(-1.90, abs=0.01)  # WHO -2 SD at birth is 2.5 kg
    assert lms.z(lms.value(3)) == pytest.approx(3.0)
    beyond = lms.z(lms.value(3) + (lms.value(3) - lms.value(2)))
    assert beyond == pytest.approx(4.0)  # one (SD3 - SD2) step above SD3
    assert growth.percentile(0) == 50.0 and growth.percentile(1.645) == pytest.approx(95, 0.01)


def test_assess_ages_and_sexes(tables):
    a = tables.assess("M", 0, 2.5)
    assert a["exclusion"] == "weight < P5 for age" and not a["implausible"]
    assert tables.assess("F", 365, 8.9)["exclusion"] is None
    ten = int(120 * growth.DAYS_PER_MONTH)
    assert tables.assess("M", ten, 31.2)["percentile"] == pytest.approx(50, abs=1)
    assert tables.assess("M", ten, 60)["implausible"] is True
    assert tables.assess("M", int(11 * 365.25), 40) is None  # beyond 10 y: no WHO table
    assert tables.assess(None, 0, 3.3) is None and tables.assess("M", 0, None) is None


def test_apply_exclusions_keeps_other_reasons(session, tables, tmp_path, monkeypatch):
    monkeypatch.setattr(growth, "tables", lambda directory=None: tables)
    boy = Patient(pseudo_id="P-G1", sex="M")
    session.add(boy)
    session.flush()
    t = dt.datetime(2026, 1, 5)
    small = Study(study_uid="g.1", modality="CT", study_datetime=t, patient_id=boy.id,
                  age_days=0, weight_kg=2.4, source_kind="test")
    normal = Study(study_uid="g.2", modality="CT", study_datetime=t, patient_id=boy.id,
                   age_days=0, weight_kg=3.4, source_kind="test",
                   drl_exclusion="weight < P5 for age")
    manual = Study(study_uid="g.3", modality="CT", study_datetime=t, patient_id=boy.id,
                   age_days=0, weight_kg=2.4, source_kind="test", drl_exclusion="premature")
    session.add_all([small, normal, manual])
    session.flush()
    stats = growth.apply_exclusions(session)
    assert small.drl_exclusion == "weight < P5 for age"
    assert normal.drl_exclusion is None and stats["cleared"] == 1
    assert manual.drl_exclusion == "premature"  # a person's reason is kept
