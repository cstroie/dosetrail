"""ESAK without a DAP meter (Order 1255/2016 annex 2)."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from dozimetrix import esak
from dozimetrix.db.models import Device, DeviceOutput, Study
from dozimetrix.db.models import Event as Ev

CURVE = [(60.0, 0.03), (80.0, 0.06), (100.0, 0.09)]  # mGy/mAs at 50 cm


def test_patient_thickness_follows_annex_2():
    # Tp = sqrt(4 G / (rho x pi x H)), G kg, H cm, rho 1e-3 kg/cm3: 70 kg, 170 cm
    assert esak.thickness_cm(70, 1.70) == pytest.approx(math.sqrt(4 * 70 / (1e-3 * math.pi * 170)))
    assert 22 < esak.thickness_cm(70, 1.70) < 24


def test_back_scatter_factor_is_read_from_table_2():
    assert esak.bsf(70, 20) == (1.33, "annex2")  # a table cell
    f, m = esak.bsf(75, 20)  # between 70 (1.33) and 80 (1.35)
    assert f == pytest.approx(1.34) and m == "annex2"
    assert esak.bsf(70, 12.5)[0] == pytest.approx((1.29 + 1.32) / 2)  # between 10 and 15 cm
    assert esak.bsf(70, 50)[0] == 1.35  # held at the table's edge (30 cm: 1.35)
    assert esak.bsf(70, None) == (1.35, "annex2_bsf")  # no field size: the annex's 1.3 to 1.4


def test_output_is_interpolated_and_never_extrapolated():
    assert esak.output_at(CURVE, 70) == pytest.approx(0.045)
    assert esak.output_at(CURVE, 59) is None and esak.output_at(CURVE, 101) is None
    assert esak.output_at([], 80) is None


def test_esak_formula():
    t = esak.thickness_cm(20, 1.15)
    expect = 0.045 * 10 * (50 / (100 - t)) ** 2 * 1.33
    val, method = esak.esak(70, 10, 100, 20, 1.15, CURVE, 50, 200, 200)
    assert val == pytest.approx(expect, rel=1e-3) and method == "annex2"
    assert esak.esak(70, 10, 100, 20, None, CURVE, 50) is None  # height unknown
    assert esak.esak(70, 10, 10, 20, 1.15, CURVE, 50) is None  # detector inside the patient
    assert esak.esak(50, 10, 100, 20, 1.15, CURVE, 50) is None  # outside the measured curve


def test_update_only_where_there_is_no_dap_meter_and_keeps_the_device_value(session):
    dev = Device(display_name="Mobile", identity_key="m", is_local=True)
    session.add(dev)
    session.flush()
    session.add_all(DeviceOutput(device_id=dev.id, kvp=k, mgy_per_mas=y) for k, y in CURVE)
    st = Study(study_uid="e.1", modality="DX", device_id=dev.id, source_kind="test",
               study_datetime=dt.datetime(2026, 2, 3), weight_kg=20.0, height_m=1.15)
    st.events = [
        Ev(kvp=70, mas=10, sid_mm=1000, field_w_mm=200, field_h_mm=200),  # calculated
        Ev(kvp=70, mas=10, sid_mm=1000, dap=0.05),  # has a DAP meter: left alone
        Ev(kvp=70, mas=10, sid_mm=1000, esd=0.4),  # the device's own value stays in esd
        Ev(kvp=70, sid_mm=1000),  # no mAs
    ]
    session.add(st)
    session.flush()
    assert esak.update(session) == 2  # the first and the third (esd is not replaced)
    calc = [e.esak_calc for e in st.events]
    assert calc[0] is not None and calc[1] is None and calc[3] is None
    assert st.events[2].esd == 0.4 and st.events[0].esak_method == "annex2"
    assert esak.update(session) == 0  # nothing changes on a second pass
    session.query(DeviceOutput).delete()
    assert esak.update(session) == 2  # no curve: the calculated values are taken back
    assert all(e.esak_calc is None for e in st.events)


def test_esak_curve_command_sets_shows_and_clears(tmp_path, capsys):
    from dozimetrix import cli
    from dozimetrix.db.migrate import upgrade
    from dozimetrix.db.session import make_engine, make_sessionmaker

    url = f"sqlite:///{tmp_path / 'c.sqlite3'}"
    upgrade(url)
    with make_sessionmaker(make_engine(url))() as s:
        s.add(Device(display_name="Mobile", identity_key="m", is_local=True))
        s.commit()
    f = tmp_path / "curve.csv"
    f.write_text("kvp,mgy_per_mas\n60,0.03\n80,0.06\n100,0.09\n")
    cfg = tmp_path / "dozimetrix.toml"
    cfg.write_text(f'[security]\nsecret_file = "{tmp_path / "secret"}"\n')
    base = ["--config", str(cfg), "esak-curve", "--url", url]
    assert cli.main([*base, "set", "--device", "1", "--file", str(f)]) == 0
    assert cli.main([*base, "show"]) == 0
    assert "60 kV 0.03" in capsys.readouterr().out
    assert cli.main([*base, "set", "--device", "1", "--file", str(f), "--fdd", "100"]) == 0
    assert cli.main([*base, "set", "--device", "9", "--file", str(f)]) == 2  # no such device
    assert cli.main([*base, "clear", "--device", "1"]) == 0
    capsys.readouterr()
    assert cli.main([*base, "show"]) == 0 and capsys.readouterr().out == ""


def test_update_loads_nothing_when_there_is_no_curve_and_no_calculated_value(session):
    dev = Device(display_name="Plain", identity_key="pl", is_local=True)
    session.add(dev)
    session.flush()
    st = Study(study_uid="e.2", modality="DX", device_id=dev.id, source_kind="test",
               study_datetime=dt.datetime(2026, 2, 3), weight_kg=20.0, height_m=1.15)
    st.events = [Ev(kvp=70, mas=10, sid_mm=1000) for _ in range(5)]
    session.add(st)
    session.flush()
    seen = []

    from sqlalchemy import event as sa_event

    @sa_event.listens_for(session.get_bind(), "before_cursor_execute")
    def count(conn, cursor, statement, *a):
        seen.append(statement)

    assert esak.update(session) == 0
    assert not any("FROM event" in q and "JOIN study" in q and "WHERE" in q
                   and "esak_calc IS NOT NULL" not in q for q in seen)


def test_esak_curve_rejects_a_malformed_line_but_allows_header_and_blanks(tmp_path, capsys):
    from dozimetrix import cli
    from dozimetrix.db.migrate import upgrade
    from dozimetrix.db.session import make_engine, make_sessionmaker

    url = f"sqlite:///{tmp_path / 'm.sqlite3'}"
    upgrade(url)
    with make_sessionmaker(make_engine(url))() as s:
        s.add(Device(display_name="Mobile", identity_key="m", is_local=True))
        s.commit()
    cfg = tmp_path / "dozimetrix.toml"
    cfg.write_text(f'[security]\nsecret_file = "{tmp_path / "secret"}"\n')
    base = ["--config", str(cfg), "esak-curve", "--url", url, "set", "--device", "1", "--file"]
    ok = tmp_path / "ok.csv"
    ok.write_text("kvp,mgy_per_mas\n\n60,0.03\n80,0.06\n")
    assert cli.main([*base, str(ok)]) == 0
    bad = tmp_path / "bad.csv"
    bad.write_text("kvp,mgy_per_mas\n60,0.03\n8O,0.06\n100,0.09\n")  # a letter O: a typo
    assert cli.main([*base, str(bad)]) == 2
    assert "line 3" in capsys.readouterr().err


def test_cli_export_refuses_a_query_that_does_not_parse(tmp_path, capsys):
    from dozimetrix import cli
    from dozimetrix.db.migrate import upgrade

    url = f"sqlite:///{tmp_path / 'q.sqlite3'}"
    upgrade(url)
    cfg = tmp_path / "dozimetrix.toml"
    cfg.write_text(f'[security]\nsecret_file = "{tmp_path / "secret"}"\n')
    out = tmp_path / "x.csv"
    base = ["--config", str(cfg), "export", "--url", url, "-o", str(out)]
    assert cli.main([*base, "-q", "modalty:CT"]) == 2  # a typo: not an export of everyone
    assert "query not understood" in capsys.readouterr().err and not out.exists()
    assert cli.main([*base, "-q", "modality:CT"]) == 0 and out.exists()
