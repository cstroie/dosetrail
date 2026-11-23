"""The spec engine: loading, roles, plugins, writers, the API and the CLI."""

from __future__ import annotations

import datetime as dt
import io
import json
import textwrap
import zipfile

import pytest

from dosimetrix.db.models import Patient, Study
from dosimetrix.specs import loader, registry, run, writers
from dosimetrix.specs.loader import SpecError

SPEC = textwrap.dedent("""
    [spec]
    id = "t-cohort"
    kind = "records"
    version = "7"
    title = { en = "Test cohort", ro = "Cohortă de test" }
    authority = "Test authority"
    country = "XX"
    legal = "Test act art. 1"
    formats = ["xlsx", "csv", "json"]
    access = ["exporter"]

    [params]
    period = { type = "range" }
    q = { type = "query", default = "" }

    [rules]
    cap = { value = 5, article = "art. 2" }

    [unit]
    level = "study"
    limit = "${rules.cap}"

    [[sheet]]
    name = "S"
    [[sheet.column]]
    header = "Study"
    field = "study.id"
    [[sheet.column]]
    header = "Sex"
    field = "patient.sex"
""")


def put(folder, name, text):
    p = folder / name
    p.write_text(text)
    return p


# ------------------------------------------------------------------ loader


def test_a_valid_spec_resolves_its_rules(tmp_path):
    p = put(tmp_path, "t.toml", SPEC)
    s = loader.parse(p)
    assert (s.id, s.kind, s.version) == ("t-cohort", "records", "7")
    assert s.rules == {"cap": 5} and s.rules_meta["cap"]["article"] == "art. 2"
    assert s.body["unit"]["limit"] == 5  # "${rules.cap}" kept its type
    assert s.label("ro") == "Cohortă de test" and s.label("fr") == "Test cohort"


@pytest.mark.parametrize("old,new,err", [
    ('kind = "records"', 'kind = "nope"', "unknown kind"),
    ('field = "study.id"', 'field = "study.nope"', "unknown field"),
    ('limit = "${rules.cap}"', 'limit = "${rules.gone}"', "unknown rule"),
    ('formats = ["xlsx", "csv", "json"]', 'formats = ["docx"]', "formats are"),
    ('period = { type = "range" }', 'period = { type = "decade" }', "period.type"),
    ('id = "t-cohort"', 'id = "Bad Id"', "id is"),
])
def test_a_bad_spec_says_what_is_wrong(tmp_path, old, new, err):
    p = put(tmp_path, "t.toml", SPEC.replace(old, new))
    with pytest.raises(SpecError, match=err):
        loader.parse(p)


def test_a_bad_file_is_a_problem_not_a_crash_and_a_dropped_file_appears(tmp_path):
    put(tmp_path, "bad.toml", "[spec]\nid = 'x'")
    cat = loader.discover(tmp_path)
    assert any("bad.toml" in p["file"] for p in cat.problems)
    assert "dosimetrix-cohort" in cat.specs  # the bundled one still works
    put(tmp_path, "t.toml", SPEC)
    assert "t-cohort" in loader.discover(tmp_path).specs  # no restart needed


def test_a_site_spec_replaces_the_bundled_one_with_the_same_id(tmp_path):
    put(tmp_path, "mine.toml", SPEC.replace("t-cohort", "dosimetrix-cohort"))
    s = loader.discover(tmp_path).specs["dosimetrix-cohort"]
    assert not s.bundled and s.version == "7"


def test_parameters_and_windows(tmp_path):
    s = loader.parse(put(tmp_path, "t.toml", SPEC))
    assert loader.window(s, {"from": "2026-02-01", "to": "2026-02-28"}) == (
        dt.datetime(2026, 2, 1), dt.datetime(2026, 3, 1))
    with pytest.raises(SpecError):
        loader.window(s, {"from": "x"})
    assert loader.check_params(s, {})["q"] == ""  # the default
    quarterly = loader.parse(put(tmp_path, "q.toml", SPEC.replace('"range"', '"quarter"')
                                 .replace("t-cohort", "t-q")))
    assert loader.window(quarterly, {"year": 2026, "quarter": 4}) == (
        dt.datetime(2026, 10, 1), dt.datetime(2027, 1, 1))
    with pytest.raises(SpecError, match="quarter"):
        loader.window(quarterly, {"year": 2026, "quarter": 9})


# ------------------------------------------------------------------ roles


def test_identifying_fields_need_pid_whatever_the_spec_says(tmp_path):
    plain = loader.parse(put(tmp_path, "t.toml", SPEC))
    assert not run.needs_pid(plain)
    run.check_roles(plain, {"exporter"})
    with pytest.raises(run.Forbidden):
        run.check_roles(plain, {"viewer"})
    run.check_roles(plain, {"admin"})  # admin always may
    named = loader.parse(put(tmp_path, "n.toml", SPEC.replace("patient.sex", "patient.name")
                             .replace("t-cohort", "t-named")))
    assert run.needs_pid(named)
    with pytest.raises(run.Forbidden, match="pid"):
        run.check_roles(named, {"exporter", "admin"})  # even an admin needs pid for identifiers
    run.check_roles(named, {"exporter", "pid"})
    text = SPEC.replace('access = ["exporter"]', "access = []")
    text = text.replace("patient.sex", "patient.name").replace("t-cohort", "t-open")
    open_ = loader.parse(put(tmp_path, "o.toml", text))
    with pytest.raises(run.Forbidden, match="role"):
        run.check_roles(open_, {"viewer", "pid"})  # access = []: only an admin may run it
    run.check_roles(open_, {"admin", "pid"})


# ------------------------------------------------------------------ plugins and registry


def test_a_plugin_registers_a_field_a_spec_can_use(tmp_path):
    from dosimetrix.specs import plugins

    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "ok.py").write_text(textwrap.dedent('''
        from dosimetrix.specs.registry import field, transform

        @field("test.constant")
        def constant(row):
            "Always 42."
            return 42

        @transform("double")
        def double(v):
            return v * 2
    '''))
    (plug / "broken.py").write_text("raise RuntimeError('nope')")
    try:
        got = plugins.load(plug)
        assert [bool(g["error"]) for g in got] == [True, False]  # broken.py first (sorted)
        assert len(got[1]["sha256"]) == 64 and "nope" in got[0]["error"]
        spec = loader.parse(put(tmp_path, "p.toml", SPEC.replace('field = "patient.sex"',
                             'field = "test.constant"\n    transform = "double"')
                                .replace("t-cohort", "t-plug")))
        assert spec.id == "t-plug"
    finally:
        registry.FIELDS.pop("test.constant", None)
        registry.TRANSFORMS.pop("double", None)


# ------------------------------------------------------------------ running and writing


@pytest.fixture
def populated(session):
    p = Patient(sex="F", pseudo_id="P-1", name="X^Y")
    session.add(p)
    session.flush()
    for i in range(8):
        session.add(Study(study_uid=f"s.{i}", modality="CT", source_kind="test", patient_id=p.id,
                          study_datetime=dt.datetime(2026, 3, 1 + i), total_dlp=100.0 + i))
    session.flush()
    return session


def _ctx(session, spec, **params):
    return run.build(session, spec, {"from": "2026-03-01", "to": "2026-03-31", **params},
                     now=dt.datetime(2026, 4, 1), username="tester")


def test_run_applies_the_row_limit_from_the_rules_and_writes_every_format(populated, tmp_path):
    spec = loader.parse(put(tmp_path, "t.toml", SPEC))
    ctx = _ctx(populated, spec)
    res = run.run(ctx)
    sheet = res.books[0].sheets[0]
    assert sheet.columns == ["Study", "Sex"] and len(sheet.rows) == 5  # limit = rules.cap
    buf = io.BytesIO()
    run.write(res, "xlsx", buf, ctx)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        wb = z.read("xl/workbook.xml").decode()
        info = z.read("xl/worksheets/sheet1.xml").decode()
    assert "Informatii" in wb and "t-cohort v7" in info and "art. 2" in info  # edition and article
    out = io.BytesIO()
    run.write(res, "csv", out, ctx)
    assert out.getvalue().decode("utf-8-sig").splitlines()[0] == "Study,Sex"
    js = writers.as_json(res, ctx)
    assert js["spec"]["id"] == "t-cohort" and js["books"][0]["sheets"][0]["rows"][0][1] == "F"
    with pytest.raises(SpecError, match="available as"):
        run.write(res, "zip", io.BytesIO(), ctx)
    json.dumps(js, default=str)


def test_the_query_narrows_the_selection(populated, tmp_path):
    text = SPEC.replace('default = ""', 'default = "modality:MG"')
    spec = loader.parse(put(tmp_path, "t.toml", text))
    assert run.run(_ctx(populated, spec)).books[0].sheets[0].rows == []
    assert len(run.run(_ctx(populated, spec, q="modality:CT")).books[0].sheets[0].rows) == 5


# ------------------------------------------------------------------ the national exports as specs


def _paediatric(session):
    from dosimetrix import classify
    from dosimetrix.db.models import Event
    from dosimetrix.seeds.loader import load_all

    load_all(session)
    kid = Patient(pseudo_id="P-K", sex="F")
    session.add(kid)
    session.flush()
    t = dt.datetime(2026, 10, 5, 9)
    head = Study(study_uid="v.1", modality="CT", study_datetime=t, patient_id=kid.id,
                 age_days=300, weight_kg=10.0, source_kind="test",
                 study_description="Head^CEREBRAL_SPIRAL_NATIV (Child)")
    head.events = [Event(protocol="Head", ctdivol=20.0, scan_length_mm=100.0, dlp=200.0,
                         ct_phantom_cm=16, mas=150.0, pitch=0.6)]
    session.add(head)
    session.flush()
    classify.run(session)


def test_the_bundled_national_exports_are_specs_with_their_dictionary():
    cat = loader.discover(None)
    assert not cat.problems
    survey, dataset = cat.specs["ro-sdnp-paediatric-survey"], cat.specs["dosimetrix-drl-dataset"]
    assert survey.version == "2026.1" and survey.rules["max_age_years"] == 18
    assert len(survey.body["column"]) == 47 and len(dataset.body["column"]) == 33
    assert survey.rules_meta["max_age_years"]["article"]  # the number says where it comes from
    assert not run.needs_pid(survey) and not run.needs_pid(dataset)  # pseudonymised, no PID


def test_the_survey_spec_runs_to_the_same_rows_as_the_old_function(session):
    from dosimetrix.export import survey as old

    _paediatric(session)
    spec = loader.discover(None).specs["ro-sdnp-paediatric-survey"]
    ctx = run.build(session, spec, {"month": "2026-10", "q": ""}, secret=b"s" * 32,
                    now=dt.datetime(2026, 11, 1), username="t")
    res = run.run(ctx)
    data, dictionary = res.books[0].sheets
    assert data.name == "Data" and dictionary.name == "Dictionary"
    assert [r[0] for r in dictionary.rows] == old.FIELDS and dictionary.rows[3][2]  # described
    want = [list(r.values()) for r in old.rows(session, [Study.study_datetime >= dt.datetime(
        2026, 10, 1), Study.study_datetime < dt.datetime(2026, 11, 1)], b"s" * 32)]
    assert data.rows == want and len(want) == 1
    assert dict(zip(data.columns, data.rows[0], strict=True))["check_dlp"] == "ok"
    out = io.BytesIO()
    run.write(res, "csv", out, ctx)
    assert out.getvalue().decode("utf-8-sig").splitlines()[0].startswith("survey_version,")
    assert "check_dlp" in out.getvalue().decode("utf-8-sig").splitlines()[0]  # the data sheet
    assert len(out.getvalue().decode("utf-8-sig").splitlines()) == 2  # header and one row


def test_the_dataset_spec_runs_through_the_engine(session):
    _paediatric(session)
    spec = loader.discover(None).specs["dosimetrix-drl-dataset"]
    ctx = run.build(session, spec, {"from": "2026-10-01", "to": "2026-10-31", "q": "",
                                    "facility": "HOSP"}, secret=b"s" * 32,
                    now=dt.datetime(2026, 11, 1), username="t")
    data = run.run(ctx).books[0].sheets[0]
    row = dict(zip(data.columns, data.rows[0], strict=True))
    assert row["dataset_version"] == "1.0" and row["facility"].startswith("F-")
    assert row["period"] == "2026-10" and row["modality"] == "CT"


# ------------------------------------------------------------------ comparison kinds as specs


def test_the_comparison_specs_are_bundled_with_their_rules():
    cat = loader.discover(None)
    assert not cat.problems
    want = {"ro-ms1245-drl-trend": ("series", "quarters", 6),
            "dosimetrix-operator-doses": ("outliers", "ratio_flag", 1.3),
            "dosimetrix-icrp135-grid": ("compare", "min_n", 10),
            "dosimetrix-icrp135-summary": ("compare", "min_n", 20)}
    for sid, (kind, rule, value) in want.items():
        s = cat.specs[sid]
        assert s.kind == kind and s.rules[rule] == value and s.rules_meta[rule]["article"]
        assert not run.needs_pid(s)


@pytest.mark.parametrize("sid,params,sheets", [
    ("ro-ms1245-drl-trend", {"year": 2026, "quarter": 3}, ["Tendinte"]),
    ("dosimetrix-operator-doses", {"year": 2026, "quarter": 3}, ["Operatori", "Acoperire"]),
    ("dosimetrix-icrp135-grid", {"year": 2026, "quarter": 3, "estimated": "true"},
     ["Cap (varsta)", "Corp (greutate)"]),
    ("dosimetrix-icrp135-summary", {"from": "2026-07-01", "to": "2026-09-30"},
     ["ICRP 135 summary"]),
])
def test_each_comparison_spec_runs_through_the_engine(session, sid, params, sheets):
    from dosimetrix.seeds.loader import load_all

    load_all(session)
    spec = loader.discover(None).specs[sid]
    ctx = run.build(session, spec, {"q": "", **params}, now=dt.datetime(2026, 11, 1),
                    username="t")
    res = run.run(ctx)
    assert [s.name for s in res.books[0].sheets] == sheets
    buf = io.BytesIO()
    run.write(res, "xlsx", buf, ctx)
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as z:
        info = z.read("xl/worksheets/sheet1.xml").decode()
    assert sid in info  # every file names the spec and its edition
    json.dumps(writers.as_json(res, ctx), default=str)


def test_a_threshold_comes_from_the_spec_not_the_code(session, tmp_path):
    from dosimetrix.seeds.loader import load_all

    load_all(session)
    text = (loader.BUNDLED / "dosimetrix-operator-doses.toml").read_text()
    text = text.replace('value = 1.3,', 'value = 2.5,').replace('id = "dosimetrix-operator-doses"',
                                                               'id = "strict-operators"')
    spec = loader.parse(put(tmp_path, "strict.toml", text))
    assert spec.rules["ratio_flag"] == 2.5 and spec.body["outliers"]["ratio_flag"] == 2.5
    ctx = run.build(session, spec, {"year": 2026, "quarter": 3, "q": ""},
                    now=dt.datetime(2026, 11, 1))
    assert "2.5" in dict(run.run(ctx).books[0].info)["Semnalare"]


# ------------------------------------------------------------------ drop a file, get the export


def test_a_dropped_plugin_and_spec_become_an_export_in_a_running_app(tmp_path):
    """The whole promise: a site folder with a spec and a plugin, a running app, no code change."""
    from fastapi.testclient import TestClient

    from dosimetrix.api.app import create_app
    from dosimetrix.config import Settings
    from dosimetrix.db.migrate import upgrade
    from dosimetrix.db.models import User
    from dosimetrix.db.session import make_engine, make_sessionmaker
    from dosimetrix.security import hash_password

    url = f"sqlite:///{tmp_path / 'site.sqlite3'}"
    upgrade(url)
    engine = make_engine(url)
    with make_sessionmaker(engine)() as s:
        s.add_all([User(username="boss", password_hash=hash_password("boss-pass-123"),
                        roles="viewer,admin"),
                   User(username="exp", password_hash=hash_password("exp-pass-1234"),
                        roles="viewer,exporter"),
                   Study(study_uid="x.1", modality="CT", source_kind="test",
                         study_datetime=dt.datetime(2026, 5, 5), total_dlp=321.0)])
        s.commit()
    specs_dir, plugins_dir = tmp_path / "specs", tmp_path / "plugins"
    specs_dir.mkdir()
    plugins_dir.mkdir()
    (plugins_dir / "hospital.py").write_text(textwrap.dedent('''
        from dosimetrix.specs.registry import field, transform

        @field("site.hospital")
        def hospital(row):
            "The name of this hospital."
            return "Spitalul Test"

        @transform("kilo")
        def kilo(v):
            return None if v is None else v / 1000
    '''))
    (plugins_dir / "broken.py").write_text("import nothing_here_exists")
    (specs_dir / "mine.toml").write_text(SPEC.replace("t-cohort", "site-cohort").replace(
        'field = "patient.sex"',
        'field = "site.hospital"\n    [[sheet.column]]\n    header = "DLP kGy.cm"\n'
        '    field = "study.total_dlp"\n    transform = "kilo"'))
    (specs_dir / "typo.toml").write_text(SPEC.replace("t-cohort", "site-typo")
                                         .replace("study.id", "study.idd"))
    app = create_app(Settings(database_url=url, specs_dir=specs_dir, plugins_dir=plugins_dir),
                     engine=engine, secret=b"k" * 32, now=dt.datetime(2026, 6, 1))
    try:
        c = TestClient(app)
        login = {"username": "boss", "password": "boss-pass-123"}
        assert c.post("/api/login", json=login).status_code == 200
        ls = c.get("/api/exports").json()
        assert "site-cohort" in {x["id"] for x in ls["specs"]}  # dropped, no restart
        assert [p["file"].rsplit("/", 1)[1] for p in ls["plugins"]] == ["broken.py", "hospital.py"]
        assert ls["plugins"][0]["error"] and ls["plugins"][1]["error"] is None  # isolated failure
        assert any("typo.toml" in p["file"] and "unknown field" in p["error"]
                   for p in ls["problems"])
        audit = c.get("/api/audit", params={"action": "plugin"}).json()["rows"]
        assert {a["target"].rsplit("/", 1)[1] for a in audit} == {"broken.py", "hospital.py"}
        assert all(len(a["detail"]["sha256"]) == 64 for a in audit)  # a changed plugin is visible
        c.post("/api/logout")
        login = {"username": "exp", "password": "exp-pass-1234"}
        assert c.post("/api/login", json=login).status_code == 200
        assert c.get("/api/exports").json()["problems"] == []  # only an admin sees the faults
        r = c.get("/api/exports/site-cohort", params={"from": "2026-05-01", "to": "2026-05-31",
                                                       "q": ""})
        sheet = r.json()["books"][0]["sheets"][0]
        assert sheet["columns"] == ["Study", "Sex", "DLP kGy.cm"]
        assert sheet["rows"] == [[sheet["rows"][0][0], "Spitalul Test", 0.321]]
        # and a spec dropped while the app runs shows up on the next listing
        (specs_dir / "later.toml").write_text(SPEC.replace("t-cohort", "site-later"))
        assert "site-later" in {x["id"] for x in c.get("/api/exports").json()["specs"]}
    finally:
        registry.FIELDS.pop("site.hospital", None)
        registry.TRANSFORMS.pop("kilo", None)


def test_the_documented_examples_stay_valid():
    """docs/examples are what a new site copies: they must load against the real registries."""
    root = loader.BUNDLED.parents[2] / "docs" / "examples"
    for name in ("cohort-ct-paediatric.toml", "monthly-return.toml"):
        spec = loader.parse(root / name)
        assert spec.id.startswith("example-") and not run.needs_pid(spec)
    monthly = loader.parse(root / "monthly-return.toml")
    from dosimetrix import deadlines

    got = deadlines.upcoming(dt.date(2026, 11, 1), (), [monthly])
    assert got and got[0]["id"].startswith("ex-monthly:") and got[0]["due"] <= "2026-11-10"
    plugin = (root / "plugin-site-fields.py").read_text()
    assert "registry import field, transform" in plugin  # the documented import still exists
    assert hasattr(registry, "field") and hasattr(registry, "transform")
