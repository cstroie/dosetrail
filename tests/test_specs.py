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
