"""Static checks for the web UI: local references resolve, sample data parses, no CDN use."""

from __future__ import annotations

import json
import re

import pytest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "dozimetrix" / "web"
PAGES = sorted(WEB.glob("*.html"))
REF = re.compile(r'(?:href|src)="([^"#?]+)')
IMPORT = re.compile(r"""^\s*import\s[^'"]*['"]([^'"]+)['"]""", re.M)


def test_pages_exist():
    assert {p.name for p in PAGES} >= {
        "index.html",
        "studies.html",
        "study.html",
        "charts.html",
        "devices.html",
    }


def test_local_references_resolve():
    for page in PAGES:
        for ref in REF.findall(page.read_text(encoding="utf-8")):
            assert not ref.startswith(("http:", "https:", "//")), (
                f"{page.name}: external reference {ref}"
            )
            assert (WEB / ref).exists(), f"{page.name}: missing {ref}"


def test_module_imports_resolve():
    for js in (WEB / "assets" / "js").rglob("*.js"):
        for ref in IMPORT.findall(js.read_text(encoding="utf-8")):
            assert ref.startswith("."), f"{js}: bare import {ref} (no npm, no CDN)"
            assert (js.parent / ref).resolve().exists(), f"{js}: missing import {ref}"


def test_no_remote_urls_in_code():
    for f in list((WEB / "assets" / "js").rglob("*.js")) + list(
        (WEB / "assets" / "css").glob("*.css")
    ):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"https?://(?!www\.w3\.org/2000/svg)", text), f"{f}: remote URL"


def test_headings_use_the_section_helpers():
    """docs/design-rules.md, "Sections and headings": no bare h1/h2 (they come from section(),
    panel(), panelHead() and pageHead() in core/dom.js), no section-title class. The first-run
    wizard has its own el() builder: there every h2 sits in an hgroup."""
    for f in (WEB / "assets" / "js").rglob("*.js"):
        if f.name == "dom.js":
            continue
        text = f.read_text(encoding="utf-8")
        assert "section-title" not in text, f"{f}: section-title is replaced by section()"
        for m in re.finditer(r"\b(h|el)\(\s*['\"]h[12]['\"]", text):
            ok = f.name == "setup.js" and text[: m.start()].endswith("el('hgroup', {}, ")
            assert ok, f"{f}: bare h1/h2, use section()/panel() (or an hgroup in the wizard)"


def test_scripts_parse_as_modules(tmp_path):
    """`node --check x.js` treats a .js file as a script and can miss a module's syntax error:
    check a .mjs copy of each page and component."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    for f in (WEB / "assets" / "js").rglob("*.js"):
        copy = tmp_path / (f.relative_to(WEB).as_posix().replace("/", "_") + ".mjs")
        copy.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
        r = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f}: {r.stderr[:300]}"


def test_sample_data_parses():
    sample = WEB / "data" / "sample"
    for name in (
        "overview.json",
        "devices.json",
        "ct-studies.json",
        "ct-acquisitions.json",
        "study-A2026112100123.json",
        "drl.json",
    ):
        data = json.loads((sample / name).read_text(encoding="utf-8"))
        assert data
    study = json.loads((sample / "study-A2026112100123.json").read_text(encoding="utf-8"))
    assert abs(sum(e["dap_gycm2"] for e in study["events"]) - study["totals"]["dap_gycm2"]) < 0.01
    assert (
        abs(sum(e["kar_mgy"] for e in study["events"]) / 1000 - study["totals"]["kar_gy"]) < 0.005
    )
