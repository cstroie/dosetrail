"""Weight / height CSV import: keys, ranks, plausibility, dry run."""

import hashlib

import pytest

from dosimetrix import sizes_import
from dosimetrix.db.models import SizeSource, Study
from dosimetrix.ingest.parse import parse
from dosimetrix.ingest.store import Store
from tests import dicom_factory as f

SECRET = b"t" * 32


def _ingest(session, uid, pid="P1", weight=None, birth="20180101"):
    r = Store(session, SECRET).store(parse(f.dx_image(
        study_uid=uid, sop_uid=uid + ".1", patient_id=pid, weight=weight, birth=birth)))
    return session.get(Study, r.study_id)


def test_dry_run_writes_nothing(session):
    st = _ingest(session, "1.8.1")
    res = sizes_import.run(session, "study_uid,weight_kg\n1.8.1,22\n")
    assert res["counts"]["studies_updated"] == 1
    session.refresh(st)
    assert st.weight_source != SizeSource.imported


def test_keys_and_sources(session):
    a = _ingest(session, "1.8.2")
    b = _ingest(session, "1.8.3", pid="P7")
    c = _ingest(session, "1.8.4", pid="P8")
    text = ("Accession;Greutate;Inaltime_cm;Sursa\n"
            f"{a.accession};21,5;118;measured\n")
    res = sizes_import.run(session, text, apply=True)
    assert res["counts"]["weight_written"] == 1 and res["counts"]["height_written"] == 1
    assert (a.weight_kg, a.weight_source, a.height_m) == (21.5, SizeSource.measured, 1.18)
    text = ("patient_id,date,modality,weight_kg,measured_at\n"
            "P7,2025-11-06,DX,23,2025-11-06 08:00\n"
            "P8,06.11.2025,,24,2025-11-04\n"
            "P9,2025-11-06,,24,\n")
    res = sizes_import.run(session, text, apply=True)
    assert b.weight_source == SizeSource.chart_same_day and b.weight_kg == 23
    assert c.weight_source == SizeSource.chart_72h
    assert res["counts"]["not_found"] == 1


def test_rank_and_plausibility(session):
    st = _ingest(session, "1.8.5")
    sizes_import.run(session, "study_uid,weight_kg,source\n1.8.5,22,measured\n", apply=True)
    res = sizes_import.run(session, "study_uid,weight_kg\n1.8.5,30\n", apply=True)
    assert st.weight_kg == 22 and res["counts"]["kept_better_source"] == 1
    st2 = _ingest(session, "1.8.6")
    res = sizes_import.run(session, "study_uid,weight_kg\n1.8.6,190\n", apply=True)
    assert res["counts"]["implausible"] == 1 and st2.weight_kg != 190


def test_hashed_accession_and_errors(session):
    st = _ingest(session, "1.8.7")
    clear = st.accession
    st.accession, st.accession_is_hashed = hashlib.sha256(clear.encode()).hexdigest(), True
    session.flush()
    res = sizes_import.run(session, f"accession,weight_kg\n{clear},20\nX,abc\n", apply=True)
    assert st.weight_kg == 20 and res["counts"]["error"] == 1
    with pytest.raises(sizes_import.RowError):
        sizes_import.run(session, "name,weight\nx,1\n")


def test_indication_columns(session):
    from dosimetrix.db.models import StudyIndication
    from dosimetrix.seeds.loader import load_all

    load_all(session)
    a = _ingest(session, "1.8.20")
    b = _ingest(session, "1.8.21")
    text = ("study_uid;indicatie;cod_indicatie\n"
            "1.8.20;Pneumonie;\n"
            "1.8.21;tuse;PED-TUS\n"
            "1.8.20;;NOPE\n")
    res = sizes_import.run(session, text, apply=True)
    assert res["counts"]["indication_written"] == 2 and res["counts"]["error"] == 1
    assert a.indication_text == "Pneumonie" and a.indication_source == "csv"
    assert session.get(StudyIndication, a.id).status == "auto"  # matched after the import
    si = session.get(StudyIndication, b.id)
    assert si.status == "manual" and si.source == "csv"
