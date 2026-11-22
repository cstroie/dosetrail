"""Static checks for the web UI: local references resolve, sample data parses, no CDN use."""

from __future__ import annotations

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "dosimetrix" / "web"
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
