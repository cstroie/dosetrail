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


# real CDC 2000 coefficients (boys and girls, 12.5 / 13.5 years; first rows of the file)
CDC = ("Sex,Agemos,L,M,S\n"
       "1,24,-0.20615245,12.6707633,0.108125811\n"
       "1,149.5,-0.773426,40.4389,0.199458\n1,150.5,-0.770879,40.9431,0.199109\n"
       "1,240,-0.16194,70.6,0.1474\n"
       "Sex,Agemos,L,M,S\n"  # the file repeats its header
       "2,24,-0.73533,12.0532,0.1071\n"
       "2,161.5,-1.20484,47.1063,0.18646\n2,162.5,-1.20484,47.5101,0.18465\n"
       "2,240,-1.51336,58.219,0.16664\n")


def test_cdc_beyond_ten_years(tmp_path):
    (tmp_path / "weianthro.txt").write_text(DAYS)
    (tmp_path / "wfawho2007.txt").write_text(MONTHS)
    (tmp_path / "wtage.csv").write_text(CDC)
    g = growth.GrowthTables.load(tmp_path)
    assert g.max_months == 240
    boy = int(150 * growth.DAYS_PER_MONTH)
    a = g.assess("M", boy, 40.9)
    assert a["source"] == "CDC" and a["percentile"] == pytest.approx(50, abs=2)
    assert g.assess("M", boy, 25)["exclusion"] == "weight < P5 for age"
    assert g.assess("F", int(162 * growth.DAYS_PER_MONTH), 90)["implausible"] is True
    assert g.assess("M", int(120 * growth.DAYS_PER_MONTH), 31.2)["source"] == "WHO"
    assert g.assess("M", int(21 * 365.25), 70) is None  # beyond 20 y: no table


def test_estimate_weights(session, tables, monkeypatch):
    from dosetrail.db.models import SizeSource

    monkeypatch.setattr(growth, "tables", lambda directory=None: tables)
    boy, nosex = Patient(pseudo_id="P-E1", sex="M"), Patient(pseudo_id="P-E2", sex=None)
    session.add_all([boy, nosex])
    session.flush()
    t = dt.datetime(2026, 1, 5)

    def st(uid, pat, age, w, src=None):
        s = Study(study_uid=uid, modality="CT", study_datetime=t, patient_id=pat.id,
                  age_days=age, weight_kg=w, weight_source=src, source_kind="test")
        session.add(s)
        return s

    missing = st("e.1", boy, 0, None)
    typo = st("e.2", boy, 0, 0.3, SizeSource.dicom_unknown)
    fine = st("e.3", boy, 0, 3.4, SizeSource.dicom_unknown)
    unknown_sex = st("e.4", nosex, 0, None)
    adult = st("e.5", boy, int(30 * 365.25), None)
    session.flush()
    out = growth.estimate_weights(session)
    assert out == {"estimated_missing": 1, "replaced_implausible": 1, "refreshed": 0}
    for s in (missing, typo, fine, unknown_sex, adult):
        session.refresh(s)
    assert missing.weight_kg == 3.3 and missing.weight_source == SizeSource.estimated_reference
    assert missing.weight_reported_kg is None
    assert typo.weight_kg == 3.3 and typo.weight_reported_kg == 0.3
    assert fine.weight_kg == 3.4 and fine.weight_source == SizeSource.dicom_unknown
    assert unknown_sex.weight_kg is None and adult.weight_kg is None
    # idempotent; the estimate is never excluded from DRL statistics (it is the median)
    assert growth.estimate_weights(session)["estimated_missing"] == 0
    growth.apply_exclusions(session)
    session.refresh(typo)
    assert typo.drl_exclusion is None


def test_known_source_weights_are_kept_unless_unrealistic(session, tables, monkeypatch):
    from dosetrail.db.models import SizeSource

    monkeypatch.setattr(growth, "tables", lambda directory=None: tables)
    boy = Patient(pseudo_id="P-K1", sex="M")
    session.add(boy)
    session.flush()
    rows = [Study(study_uid=f"k.{i}", modality="DX", study_datetime=dt.datetime(2026, 1, 5),
                  patient_id=boy.id, age_days=0, weight_kg=w, weight_source=src,
                  source_kind="test")
            for i, (w, src) in enumerate([(2.2, SizeSource.chart_same_day),   # < P1, real
                                          (2.2, SizeSource.dicom_unknown),    # < P1, unknown
                                          (0.3, SizeSource.chart_same_day)])]  # impossible
    session.add_all(rows)
    session.flush()
    assert growth.estimate_weights(session)["replaced_implausible"] == 2
    for s in rows:
        session.refresh(s)
    assert rows[0].weight_kg == 2.2 and rows[0].weight_source == SizeSource.chart_same_day
    assert rows[1].weight_source == rows[2].weight_source == SizeSource.estimated_reference
