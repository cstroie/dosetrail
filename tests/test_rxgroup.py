"""Radiography exposures on different devices meet under one anatomy, projection and band."""

from sqlalchemy import select

from dozimetrix import classify, rxgroup
from dozimetrix.db.models import Code, CodeSystem
from dozimetrix.seeds.loader import load_all


def _code(session, code):
    row = session.execute(select(Code.code, Code.meaning).join(CodeSystem).where(
        CodeSystem.key == "ro-nom-rx", Code.code == code)).one()
    return tuple(row)


def _describer(session):
    load_all(session)
    return rxgroup.Describer(session), lambda c: _code(session, c)


def test_skull_and_craniu_are_one_anatomy(session):
    d, code = _describer(session)
    cr = code("RG-CR")
    a = d.describe("W001a Craniu a.p. 1-6 ani", None, cr, 700, 12)
    b = d.describe("T001a Skull a.p. 1-6 ani", None, cr, 700, 12)
    assert a.name == b.name == "Craniu AP · 1-6 y"
    assert (a.source, b.source) == ("name", "name")


def test_name_band_wins_and_patient_band_fills_the_gap(session):
    d, code = _describer(session)
    cr, th = code("RG-CR"), code("RG-TH")
    named = d.describe("X035 Pulmonara 0-5 kg", None, th, 400, 3.2)
    assert named.name == "Toracică AP · 0-5 kg" and named.source == "name"  # chest defaults to AP
    # the Discovery: one "CRANIU" for every age; head exams by age (ICRP 135) ...
    kid = d.describe("CRANIU", None, cr, int(2 * 365.25), 12)
    assert (kid.label, kid.source, kid.kind) == ("1-6 y", "patient", "age")
    adult = d.describe("CRANIU", None, cr, int(40 * 365.25), 80)
    assert adult.label == ">18 y"
    # ... and the rest by weight, in the same words the names use
    body = d.describe("ABDOMEN", None, code("RG-AB"), 3000, 22)
    assert (body.label, body.kind, body.source) == ("15-30 kg", "weight", "patient")


def test_requested_kind_and_missing_values(session):
    d, code = _describer(session)
    cr = code("RG-CR")
    # an age-named protocol in the weight view: the patient's weight band, marked as such
    g = d.describe("W001a Craniu a.p. 1-6 ani", None, cr, 700, 12, "weight")
    assert (g.kind, g.label, g.source) == ("weight", "5-15 kg", "patient")
    none = d.describe("CRANIU", None, cr, None, None, "weight")
    assert (none.kind, none.label, none.source) == ("none", "", "")
    assert d.describe("", None, None, 10, 10) is None  # no code, no name: nothing to group


def test_projection_from_the_view_then_the_name(session):
    d, code = _describer(session)
    cr = code("RG-CR")
    assert d.describe("CRANIU", "lateral", cr, 700, 12).projection == "LAT"
    assert d.describe("X070 Mana d.v./v.d. 30-50 Kg", None, code("RG-MSMN"), 3000, 40).projection == "DV"


def test_last_resort_rule_does_not_hide_a_specific_name(session):
    load_all(session)
    clf = classify.Classifier(session)
    spine = {r.code: r.id for r in session.scalars(select(Code))}["RG-CVNS"]
    vague = [("protocol", "COLOANA"), ("study_description", "Spine")]
    assert clf.first("rx", vague) is None and clf.last_resort("rx", vague)[0] == spine
    # the Discovery names a knee "COLOANA": the study description decides
    knee = [("protocol", "COLOANA"), ("study_description", "Knee")]
    assert clf.first("rx", knee) is not None
    for text, expect in [("X110a Forefoot d.p. 15-30 Kg", "RG-MIAP"), ("X125a Calcai lat >50 Kg", "RG-MICL"),
                         ("T011R Petrous bone", "RG-CRMS")]:
        assert classify.default_rules().match("rx", classify.normalise(text)) == expect


def test_run_reclassifies_every_study_of_a_long_list(session, monkeypatch):
    """The chunk generator once sliced a list the loop had rebound: only the first batch ran."""
    import datetime as dt
    from dozimetrix.db.models import Classification, Event, Study

    load_all(session)
    monkeypatch.setattr(classify, "BATCH", 3)
    for i in range(8):
        st = Study(study_uid=f"7.7.{i}", modality="DX", study_datetime=dt.datetime(2026, 1, 1),
                   source_kind="test", n_events=1, events=[Event(protocol="W001a Craniu a.p. 1-6 ani", dap=0.1)])
        session.add(st)
    session.flush()
    ids = list(session.scalars(select(Study.id).where(Study.study_uid.like("7.7.%"))))
    assert classify.run(session, ids)["studies"] == 8
    done = {sid for (sid,) in session.execute(select(Classification.study_id).where(Classification.study_id.in_(ids)))}
    assert done == set(ids)


def test_an_exam_has_one_size_type(session):
    d, code = _describer(session)
    assert d.own_kind("W001a Craniu a.p. 1-6 ani", code("RG-CR")) == "age"
    assert d.own_kind("W035 Torace a.p. 5-15Kg", code("RG-TH")) == "weight"
    # no band in the name: head exams by age, the rest by weight (ICRP 135)
    assert d.own_kind("CRANIU", code("RG-CR")) == "age"
    assert d.own_kind("ABDOMEN", code("RG-AB")) == "weight"
    # the name decides when it states a band, whatever the exam
    assert d.own_kind("W035 Torace a.p. 3-12 luni", code("RG-TH")) == "age"


def test_bands_outside_the_scheme(session):
    d, code = _describer(session)
    ab = code("RG-AB")
    assert d.is_standard("weight", 5, 15) and d.is_standard("weight", 0, 5) and d.is_standard("weight", 110, rxprotocols_inf())
    assert not d.is_standard("weight", 0, 15) and not d.is_standard("weight", 50, rxprotocols_inf())
    assert d.is_standard("age", 12, 72) and d.is_standard("age", 0, 3) and not d.is_standard("age", 72, rxprotocols_inf())
    odd = "X060a Antebrat a.p. >50 Kg"
    keep = d.describe(odd, None, code("RG-MSAB"), 3000, 60, "weight", "keep")
    assert (keep.label, keep.source) == (">50 kg", "name")
    assert d.describe(odd, None, code("RG-MSAB"), 3000, 60, "weight", "hide").source == "hidden"
    re = d.describe(odd, None, code("RG-MSAB"), 3000, 60, "weight", "reband")
    assert (re.label, re.source) == ("50-80 kg", "patient")
    # a standard band is never touched
    assert d.describe("W035 Torace a.p. 5-15Kg", None, code("RG-TH"), 900, 10, "weight", "hide").source == "name"


def rxprotocols_inf():
    from dozimetrix.rxprotocols import INF
    return INF


def _dx_study(session, uid, protocol, age_days, weight=None, code="RG-CR"):
    import datetime as dt
    from dozimetrix.db.models import Event, Study

    st = Study(study_uid=uid, modality="DX", study_datetime=dt.datetime(2026, 2, 1), source_kind="test",
               n_events=1, age_days=age_days, weight_kg=weight, events=[Event(protocol=protocol, dap=0.1)])
    session.add(st)
    session.flush()
    return st


def test_update_stores_the_bands_and_resolve_matches_describe(session):
    from dozimetrix.db.models import ExposureBand

    load_all(session)
    kid = _dx_study(session, "8.8.1", "CRANIU", int(2 * 365.25), 12)
    named = _dx_study(session, "8.8.2", "W035 Torace a.p. 5-15Kg", int(1.5 * 365.25), 11)
    classify.run(session, [kid.id, named.id])
    stats = rxgroup.update(session, [kid.id, named.id])
    assert stats["exposures"] == 2
    k = session.scalar(select(ExposureBand).where(ExposureBand.study_id == kid.id))
    # European: head exam, 2 years -> ext-head-age 1-6y; the body weight band too; national: 1-4 y
    assert (k.age_eu, k.weight_eu, k.age_nat, k.name_kind) == ("1-6y", "5-15kg", "1-4y", None)
    n = session.scalar(select(ExposureBand).where(ExposureBand.study_id == named.id))
    assert (n.name_kind, n.name_lo, n.name_hi, n.name_label, n.projection) == ("weight", 5, 15, "5-15 kg", "AP")
    # an open band end is stored as NULL, and read back as open
    open_ = _dx_study(session, "8.8.3", "X070 Mana d.v./v.d. >50 Kg", 9000, 70)
    classify.run(session, [open_.id])
    rxgroup.update(session, [open_.id])
    o = session.scalar(select(ExposureBand).where(ExposureBand.study_id == open_.id))
    assert (o.name_lo, o.name_hi, o.name_label, o.projection) == (50, None, ">50 kg", "DV")
    # refilling replaces the rows, never duplicates them
    rxgroup.update(session, [kid.id])
    assert len(list(session.scalars(select(ExposureBand).where(ExposureBand.study_id == kid.id)))) == 1
    # reading back gives the same group as computing it
    d = rxgroup.Describer(session)
    ev = [e for e in kid.events][0]
    stored = rxgroup.load(session, [ev.id])[ev.id]
    assert d.resolve("CRANIU", stored, None).name == "Craniu · 1-6 y"
    # a weight taken from the growth chart is not the patient's: no weight band
    from dozimetrix.db.models import SizeSource

    kid.weight_source = SizeSource.estimated_reference
    session.flush()
    rxgroup.update(session, [kid.id])
    assert session.scalar(select(ExposureBand.weight_eu).where(ExposureBand.study_id == kid.id)) is None


def test_fetch_computes_what_is_not_stored_yet(session):
    load_all(session)
    st = _dx_study(session, "8.8.4", "W001a Craniu a.p. 1-6 ani", 700, 12)
    classify.run(session, [st.id])
    d = rxgroup.Describer(session)
    ev = st.events[0]
    got = rxgroup.fetch(session, d, [(ev.id, ev.protocol, None, 700, 12, None)])
    assert d.resolve(ev.protocol, got[ev.id], "age").name == "Craniu AP · 1-6 y"


def test_periodic_maintenance_runs_every_task_and_remembers_the_rules(engine):
    from dozimetrix import maintenance
    from dozimetrix.db.models import AppSetting
    from dozimetrix.db.session import make_sessionmaker
    from dozimetrix.seeds.loader import load_all as seed

    maker = make_sessionmaker(engine)
    with maker() as s:
        seed(s)
        s.commit()
    first = maintenance.run_periodic(maker, days=30)
    assert first["ok"] and first["mode"] == "full" and first["rules_changed"]  # first run: everything
    assert set(first["tasks"]) == {"reclassify", "bands", "indications", "retry-derive", "device-counters",
                                   "review-counts"}
    second = maintenance.run_periodic(maker, days=30)
    assert second["ok"] and second["mode"] == "last 30 days" and not second["rules_changed"]
    with maker() as s:
        assert s.get(AppSetting, maintenance.STATUS_KEY).value["ok"] is True
        s.get(AppSetting, maintenance.RULES_KEY).value = {"hash": "old"}  # the rules changed since
        s.commit()
    assert maintenance.run_periodic(maker, days=30)["mode"] == "full"
