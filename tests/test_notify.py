"""Tests for notify.py: alert notifications (email, ntfy)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from dozimetrix import notify
from dozimetrix.db.models import Alert, AlertRule, Device, Study


class TestNotifySettings:
    def test_defaults(self):
        cfg = notify.NotifySettings()
        assert cfg.email_to == []
        assert cfg.email_from is None
        assert cfg.smtp_host is None
        assert cfg.smtp_port == 587
        assert cfg.smtp_starttls is True
        assert cfg.smtp_ssl is False
        assert cfg.smtp_user is None
        assert cfg.smtp_password is None
        assert cfg.ntfy_url is None
        assert cfg.ntfy_token is None
        assert cfg.max_age_hours == 48
        assert cfg.timeout == 15

    def test_from_dict(self):
        raw = {
            "email_to": ["a@b.c"],
            "email_from": "d@e.f",
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_starttls": False,
            "smtp_ssl": True,
            "smtp_user": "user",
            "smtp_password": "pass",
            "ntfy_url": "https://ntfy.example.org",
            "ntfy_token": "token123",
            "max_age_hours": 24,
            "base_url": "https://dose.org",
        }
        cfg = notify.NotifySettings.from_dict(raw)
        assert cfg.email_to == ["a@b.c"]
        assert cfg.smtp_port == 465
        assert cfg.smtp_ssl is True
        assert cfg.smtp_starttls is False
        assert cfg.ntfy_url == "https://ntfy.example.org"
        assert cfg.max_age_hours == 24
        assert cfg.base_url == "https://dose.org"

    def test_channels_property(self):
        cfg = notify.NotifySettings()
        assert cfg.channels == []

        cfg.smtp_host = "smtp.example.com"
        cfg.email_to = ["a@b.c"]
        assert "email" in cfg.channels

        cfg.ntfy_url = "https://ntfy.sh"
        assert "ntfy" in cfg.channels


class TestCompose:
    def setup_method(self):
        self.rule = AlertRule(id=1, name="High DAP", kind="dap", severity="alert", note="Test note")
        self.study = Study(
            id=10,
            study_uid="1.2.3",
            study_description="CT Abdomen",
            modality="CT",
            study_datetime=datetime(2026, 1, 15, 10, 30),
            device=Device(id=1, display_name="CT-1"),
        )
        self.alert = Alert(
            id=100,
            rule_id=1,
            study_id=10,
            value=300.0,
            severity="alert",
            message="DAP 300 Gy·cm² exceeds 250",
            study_datetime=datetime(2026, 1, 15, 10, 30),
        )

    def test_compose_alert(self):
        msg = notify.compose(self.alert, self.rule, self.study, "CT-1", "https://dose.org")
        assert "[Dozimetrix] ALERT: High DAP" in msg.title
        assert "High DAP" in msg.body
        assert "DAP 300 Gy·cm² exceeds 250" in msg.body
        assert "CT Abdomen" in msg.body
        assert "CT-1" in msg.body
        assert msg.link == "https://dose.org/study.html?id=10"
        assert msg.urgent is True

    def test_compose_warning(self):
        self.alert.severity = "warning"
        self.rule.severity = "warning"
        msg = notify.compose(self.alert, self.rule, self.study, "CT-1", "https://dose.org")
        assert "[Dozimetrix] Warning: High DAP" in msg.title
        assert msg.urgent is False


class TestSendEmail:
    def setup_method(self):
        self.cfg = notify.NotifySettings(
            email_to=["physics@example.org"],
            email_from="dozimetrix@example.org",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="user",
            smtp_password="pass",
        )
        self.msg = notify.Message(
            title="[Dozimetrix] Test",
            body="Test body",
            link="https://dose.org/study.html?id=10",
            urgent=False,
        )

    @patch("dozimetrix.notify.smtplib.SMTP")
    def test_send_email_success(self, mock_smtp):
        mock_conn = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_conn

        notify.send_email(self.cfg, self.msg)

        mock_conn.send_message.assert_called_once()
        mock_conn.starttls.assert_called_once()
        mock_conn.login.assert_called_once_with("user", "pass")

    @patch("dozimetrix.notify.smtplib.SMTP")
    def test_send_email_tls_false_ssl_false(self, mock_smtp):
        mock_conn = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_conn
        cfg = notify.NotifySettings(
            email_to=["a@b.c"],
            email_from="d@e.f",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_user="u",
            smtp_password="p",
            smtp_starttls=False,
            smtp_ssl=False,
        )
        notify.send_email(cfg, self.msg)
        mock_conn.starttls.assert_not_called()

    @patch("dozimetrix.notify.smtplib.SMTP_SSL")
    def test_send_email_ssl_true(self, mock_smtp_ssl):
        mock_conn = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = mock_conn
        cfg = notify.NotifySettings(
            email_to=["a@b.c"],
            email_from="d@e.f",
            smtp_host="smtp.example.com",
            smtp_port=465,
            smtp_user="u",
            smtp_password="p",
            smtp_ssl=True,
        )
        notify.send_email(cfg, self.msg)
        mock_smtp_ssl.assert_called_once()
        args, kwargs = mock_smtp_ssl.call_args
        assert args == ("smtp.example.com", 465)
        assert kwargs["timeout"] == 15
        assert "context" in kwargs
        mock_conn.login.assert_called_once_with("u", "p")


class TestSendNtfy:
    def setup_method(self):
        self.cfg = notify.NotifySettings(
            ntfy_url="https://ntfy.sh/dozimetrix-alerts",
            ntfy_token="secret-token",
            base_url="https://dose.org",
        )
        self.msg = notify.Message(
            title="[Dozimetrix] ALERT: High DAP",
            body="Test body",
            link="https://dose.org/study.html?id=10",
            urgent=True,
        )

    @patch("dozimetrix.notify.urllib.request.urlopen")
    def test_send_ntfy_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        notify.send_ntfy(self.cfg, self.msg)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://ntfy.sh/dozimetrix-alerts"
        assert req.method == "POST"
        assert req.headers["Authorization"] == "Bearer secret-token"
        assert req.headers["Priority"] == "high"
        assert req.headers["Tags"] == "rotating_light"
        assert req.headers["Click"] == "https://dose.org/study.html?id=10"

    @patch("dozimetrix.notify.urllib.request.urlopen")
    def test_send_ntfy_failure_raises(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with pytest.raises(RuntimeError, match="ntfy answered 500"):
            notify.send_ntfy(self.cfg, self.msg)

    @patch("dozimetrix.notify.urllib.request.urlopen")
    def test_send_ntfy_no_token(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        cfg = notify.NotifySettings(ntfy_url="https://ntfy.sh/topic")
        notify.send_ntfy(cfg, self.msg)

        req = mock_urlopen.call_args[0][0]
        assert "Authorization" not in req.headers


class TestSendTest:
    def test_send_test_email(self):
        cfg = notify.NotifySettings(
            email_to=["a@b.c"],
            email_from="d@e.f",
            smtp_host="smtp.example.com",
        )
        with patch("dozimetrix.notify.send_email") as mock_send:
            # Pass custom senders to use the mock
            notify.send_test(cfg, "email", senders={"email": mock_send})
            mock_send.assert_called_once()
            msg = mock_send.call_args[0][1]
            assert "Test notification" in msg.title

    def test_send_test_ntfy(self):
        cfg = notify.NotifySettings(ntfy_url="https://ntfy.sh/topic")
        with patch("dozimetrix.notify.send_ntfy") as mock_send:
            notify.send_test(cfg, "ntfy", senders={"ntfy": mock_send})
            mock_send.assert_called_once()

    def test_send_test_unknown_channel_raises(self):
        cfg = notify.NotifySettings()
        with pytest.raises(RuntimeError, match="not configured"):
            notify.send_test(cfg, "email")


class TestLoadSettings:
    def test_load_from_toml_only(self):
        from sqlalchemy import create_engine

        from dozimetrix.db.models import Base
        from dozimetrix.db.session import make_sessionmaker

        engine = create_engine("sqlite:///:memory:")
        maker = make_sessionmaker(engine)
        with maker() as s:
            Base.metadata.create_all(engine)

        # notify dict is the [notify] table, not wrapped in "notify" key
        notify_toml = {
            "email_to": ["a@b.c"],
            "smtp_host": "smtp.example.com",
            "ntfy_url": "https://ntfy.sh/topic",
        }
        with maker() as s:
            cfg = notify.load_settings(s, notify_toml, b"x"*32)
            assert cfg.email_to == ["a@b.c"]
            assert cfg.smtp_host == "smtp.example.com"
            assert cfg.ntfy_url == "https://ntfy.sh/topic"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
