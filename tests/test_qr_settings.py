"""[qr] section of the configuration: days and interval of the qr --loop daemon."""
from dozimetrix import config
from dozimetrix.cli import build_parser


def _load(tmp_path, monkeypatch, text):
    f = tmp_path / "d.toml"
    f.write_text(text)
    monkeypatch.setattr(config, "_explicit", f)
    return config.load_settings()


def test_defaults(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, "")
    assert (s.qr_days, s.qr_interval) == (2, 720)


def test_from_toml(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, "[qr]\ndays = 5\ninterval = 300\n")
    assert (s.qr_days, s.qr_interval) == (5, 300)


def test_values_are_clamped(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, "[qr]\ndays = 0\ninterval = -1\n")
    assert (s.qr_days, s.qr_interval) == (1, 1)


def test_cli_flags_default_to_the_config():
    a = build_parser().parse_args(["qr", "--loop"])
    assert a.days is None and a.interval is None
    a = build_parser().parse_args(["qr", "--days", "3", "--interval", "60"])
    assert (a.days, a.interval) == (3, 60)
