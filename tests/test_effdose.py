"""Effective dose coefficients (none ship) and the typed-in glandularity."""

from __future__ import annotations

import pytest

from dosimetrix import effdose
from dosimetrix.db.models import EffectiveDoseCoefficient as C


def test_the_row_for_the_age_is_the_smallest_limit_at_or_above_it():
    c = effdose.Coefficients([("Tc-99m", "DMSA", 1.0, 0.0120), ("Tc-99m", "DMSA", 5.0, 0.0080),
                              ("Tc-99m", "DMSA", None, 0.0050)])
    assert c("tc-99m", "dmsa", 200) == 0.0120  # 7 months
    assert c("Tc-99m", "DMSA", 3 * 365) == 0.0080
    assert c("Tc-99m", "DMSA", 40 * 365) == 0.0050  # adult row
    assert c("Tc-99m", "DMSA", None) == 0.0050  # unknown age: the adult one
    assert c("Tc-99m", "MDP", 100) is None  # nothing entered for it
    assert not effdose.Coefficients([]) and effdose.Coefficients([]) ("x", "y", 1) is None


def test_effective_dose_command_needs_a_source_for_every_row(tmp_path, capsys):
    from dosimetrix import cli
    from dosimetrix.db.migrate import upgrade

    url = f"sqlite:///{tmp_path / 'e.sqlite3'}"
    upgrade(url)
    cfg = tmp_path / "dosimetrix.toml"
    cfg.write_text(f'[security]\nsecret_file = "{tmp_path / "secret"}"\n')
    base = ["--config", str(cfg), "effective-dose", "--url", url]
    good = tmp_path / "good.csv"
    good.write_text("nuclide,form,age_to_years,msv_per_mbq,source\n"
                    "Tc-99m,DMSA,5,0.0100,test source\nTc-99m,DMSA,,0.0050,test source\n")
    assert cli.main([*base, "set", "--file", str(good)]) == 0
    assert cli.main([*base, "show"]) == 0
    out = capsys.readouterr().out
    assert "to 5 y" in out and "adult" in out and "test source" in out
    bad = tmp_path / "bad.csv"
    bad.write_text("Tc-99m,DMSA,5,0.01,\n")  # no source
    assert cli.main([*base, "set", "--file", str(bad)]) == 2
    assert cli.main([*base, "clear"]) == 0
    capsys.readouterr()
    assert cli.main([*base, "show"]) == 0 and capsys.readouterr().out == ""


def test_none_ship_with_the_program(session):
    assert not effdose.load(session)


def test_art12_fills_the_effective_dose_only_with_a_coefficient(session):
    import datetime as dt

    from dosimetrix import classify
    from dosimetrix.db.models import Device, Patient, Study
    from dosimetrix.db.models import Event as Ev
    from dosimetrix.reports import art12
    from dosimetrix.seeds.loader import load_all

    load_all(session)
    dev = Device(display_name="Gamma", identity_key="g", is_local=True)
    pat = Patient(sex="M", pseudo_id="P-1")
    session.add_all([dev, pat])
    session.flush()
    nm = Study(study_uid="n.1", modality="NM", study_datetime=dt.datetime(2026, 2, 5),
               device_id=dev.id, patient_id=pat.id, age_days=730, source_kind="test",
               study_description="Scintigrafie renala", admin_activity_mbq=40.0)
    nm.events = [Ev(event_type="radiopharmaceutical", protocol="DMSA",
                    extra_json={"activity_mbq": 40.0})]
    session.add(nm)
    session.flush()
    classify.run(session)

    def msv():
        b = next(b for b in art12.build(session, 2026) if b.family == "nm_diagnostic")
        return next(s for s in b.sheets if s.name == "SCINTIGRAFIE RENALA").rows[0][-1]

    assert msv() == ""  # no coefficient: blank, not a guess
    session.add(C(nuclide="Tc-99m", form="DMSA", age_to_years=5.0, msv_per_mbq=0.01,
                  source="test"))
    session.flush()
    assert msv() == pytest.approx(0.4)  # 40 MBq x 0.01 mSv/MBq
