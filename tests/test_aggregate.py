"""Central survey collection: upload checks, replacement, national statistics, XLSX."""

import csv
import io
import json

import pytest

from dosetrail import aggregate
from dosetrail.export.survey import FIELDS, SURVEY_VERSION
from dosetrail.seeds.loader import load_all


def _csv(inst, period, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, FIELDS)
    w.writeheader()
    for r in rows:
        w.writerow({"survey_version": SURVEY_VERSION, "institution": inst, "period": period,
                    "modality": "CT", "ndrl_exam": "CT-CAP", "head_age_group": "1-6y",
                    "age_years": 3, "phantom": 16, **r})
    return buf.getvalue().encode("utf-8-sig")


@pytest.fixture
def two(session):
    load_all(session)
    a = _csv("I-AAAA", "2026-10", [{"dlp": v, "ctdivol": v / 20} for v in
                                   (300, 320, 340, 360, 380, 400, 420, 440, 460, 480)]
             + [{"dlp": "-5"}, {"dlp": ""}])  # a negative DLP and an empty row: rejected
    b = _csv("I-BBBB", "2026-10", [{"dlp": v} for v in (500, 520, 540, 560, 580, 600, 620,
                                                         640, 660, 680)]
             + [{"dlp": 150, "phantom": 32}])  # head on the 32 cm phantom: left out
    ua = aggregate.upload(session, a, filename="a.csv", user="central")
    ub = aggregate.upload(session, b, filename="b.csv")
    return ua, ub


def test_upload_checks(session, two):
    ua, _ub = two
    assert ua.n_rows == 10 and ua.n_rejected == 2
    reasons = " ".join(i["reason"] for i in ua.report["issues"])
    assert "dlp" in reasons and "without DLP" in reasons
    with pytest.raises(aggregate.SurveyError, match="uploaded already"):
        aggregate.upload(session, _csv("I-AAAA", "2026-10", [{"dlp": 1}] * 0 + [
            {"dlp": v, "ctdivol": v / 20} for v in (300, 320, 340, 360, 380, 400, 420, 440,
                                                     460, 480)] + [{"dlp": "-5"}, {"dlp": ""}]))
    with pytest.raises(aggregate.SurveyError, match="one institution"):
        aggregate.upload(session, _csv("X", "2026-10", [{"dlp": 1}]) + _csv(
            "Y", "2026-10", [{"dlp": 1}]).split(b"\r\n", 1)[1])
    with pytest.raises(aggregate.SurveyError, match="missing column"):
        aggregate.upload(session, b"a,b\n1,2\n")


def test_replacement_and_national(session, two):
    rep = aggregate.national(session, "2026-01", "2026-12")
    (dlp,) = [r for r in rep["rows"] if r["quantity"] == "dlp"]
    assert dlp["group"] == "1-6y" and dlp["n"] == 20 and dlp["n_institutions_counted"] == 2
    # medians 390 and 590 -> P75 of [390, 590] = 540
    assert dlp["ndrl_proposal"] == pytest.approx(540) and dlp["edrl"]
    assert dlp["status_edrl"] in aggregate.STATUS and dlp["iq_flag"] == "ok"
    assert dlp["n_phantom32"] == 1
    # a new upload for the same institution and month replaces the old one
    aggregate.upload(session, _csv("I-AAAA", "2026-10", [{"dlp": 100}] * 10))
    dlp = [r for r in aggregate.national(session, "2026-01", "2026-12")["rows"]
           if r["quantity"] == "dlp"][0]
    assert dlp["n"] == 20 and dlp["institutions"][0]["median"] == 100
    assert [u["status"] for u in aggregate.uploads(session)].count("replaced") == 1
    nat = aggregate.national(session, "2026-01", "2026-12", grid="national")
    assert {r["group"] for r in nat["rows"]} == {"1-4y"}
    buf = io.BytesIO()
    aggregate.write_xlsx(rep, buf, {"Generat": "test"})
    assert buf.getvalue()[:2] == b"PK"


def test_json_upload(session):
    load_all(session)
    doc = {"dataset": "dosetrail-paediatric-survey", "version": SURVEY_VERSION,
           "rows": [{"survey_version": SURVEY_VERSION, "institution": "I-J", "period": "2026-09",
                     "modality": "DX", "ndrl_exam": "RX-TORACE-PA-AP", "weight_group": "5-15kg",
                     "weight_source": "estimated_reference", "pka": 20.0}]}
    up = aggregate.upload(session, json.dumps(doc).encode(), filename="x.json")
    assert up.n_rows == 1
    rep = aggregate.national(session, "2026-09", "2026-09")
    assert rep["counts"]["no_group"] == 1  # estimated weight left out by default
    rep = aggregate.national(session, "2026-09", "2026-09", estimated_weights=True)
    assert rep["rows"][0]["quantity"] == "dap" and rep["rows"][0]["n"] == 1
