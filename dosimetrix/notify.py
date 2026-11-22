"""Alert notifications: e-mail (SMTP, stdlib smtplib) and ntfy (HTTP POST, stdlib urllib).

* Only alerts whose rule has ``notify`` set, and whose study is recent (``max_age_hours``,
  default 48), are sent. Older alerts, e.g. raised by a backfill or a batch evaluation, are
  marked "not sent (historical)" so nobody gets a flood of old news.
* Messages carry **no patient identifiers**: the rule, the value, the exam, the device, the
  study date and a link to the study, which needs a login.
* A failure is recorded on the alert (``notify_error``) and retried on the next run; after
  5 failed runs it is given up.

Configuration (dosimetrix.toml)::

    [notify]
    base_url = "https://dose.example.org"      # for links in messages
    email_to = ["physics@example.org"]
    email_from = "dosimetrix@example.org"
    smtp_host = "smtp.example.org"
    smtp_port = 587
    smtp_starttls = true
    smtp_user = "dosimetrix"
    smtp_password_file = "/etc/dosimetrix/smtp-password"
    ntfy_url = "https://ntfy.example.org/dosimetrix-alerts"
    ntfy_token_file = "/etc/dosimetrix/ntfy-token"
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import smtplib
import ssl
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .alerts import DEVICE_KINDS
from .db.models import Alert, AlertRule, Device, Study

log = logging.getLogger(__name__)
MAX_TRIES = 5


@dataclass
class NotifySettings:
    base_url: str | None = None
    email_to: list[str] = field(default_factory=list)
    email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_user: str | None = None
    smtp_password_file: Path | None = None
    ntfy_url: str | None = None
    ntfy_token_file: Path | None = None
    max_age_hours: float = 48
    timeout: float = 15

    @classmethod
    def from_dict(cls, raw: dict) -> NotifySettings:
        s = cls()
        for k, v in raw.items():
            if hasattr(s, k):
                if k.endswith("_file") and v:
                    v = Path(v).expanduser()
                setattr(s, k, v)
        return s

    @property
    def channels(self) -> list[str]:
        out = []
        if self.smtp_host and self.email_to:
            out.append("email")
        if self.ntfy_url:
            out.append("ntfy")
        return out


def _secret(path: Path | None) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path and path.is_file() else None


@dataclass
class Message:
    title: str
    body: str
    link: str | None
    urgent: bool


def compose(a: Alert, rule: AlertRule, s: Study, device: str | None, base_url: str | None
            ) -> Message:
    link = f"{base_url.rstrip('/')}/study.html?id={s.id}" if base_url else None
    when = s.study_datetime.strftime("%Y-%m-%d %H:%M") if s.study_datetime else "?"
    if rule.kind in DEVICE_KINDS:  # about the device; the study is its latest one
        seen = a.study_datetime.strftime("%Y-%m-%d %H:%M") if a.study_datetime else "?"
        lines = [rule.name, "", a.message, "", f"Device: {device or '—'}", f"Seen: {seen}",
                 f"Latest study: {s.modality} · {when} · id {s.id}"]
    else:
        lines = [rule.name, "", a.message, "",
                 f"Study: {s.modality} · {s.study_description or '—'}",
                 f"Device: {device or '—'}", f"Date: {when}", f"Study id: {s.id}"]
    if link:
        lines += ["", link]
    if rule.note:
        lines += ["", rule.note]
    level = "ALERT" if a.severity == "alert" else "Warning"
    return Message(f"[Dosimetrix] {level}: {rule.name}", "\n".join(lines), link,
                   a.severity == "alert")


def send_email(cfg: NotifySettings, m: Message) -> None:
    msg = EmailMessage()
    msg["Subject"] = m.title
    msg["From"] = cfg.email_from or (cfg.smtp_user or "dosimetrix")
    msg["To"] = ", ".join(cfg.email_to)
    if m.urgent:
        msg["X-Priority"] = "1"
    msg.set_content(m.body)
    ctx = ssl.create_default_context()
    smtp_cls = smtplib.SMTP_SSL if cfg.smtp_ssl else smtplib.SMTP
    kwargs = {"context": ctx} if cfg.smtp_ssl else {}
    with smtp_cls(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout, **kwargs) as smtp:
        if cfg.smtp_starttls and not cfg.smtp_ssl:
            smtp.starttls(context=ctx)
        password = _secret(cfg.smtp_password_file)
        if cfg.smtp_user and password:
            smtp.login(cfg.smtp_user, password)
        smtp.send_message(msg)


def send_ntfy(cfg: NotifySettings, m: Message) -> None:
    headers = {"Title": m.title.encode("utf-8").decode("latin-1", "replace"),
               "Priority": "high" if m.urgent else "default",
               "Tags": "rotating_light" if m.urgent else "warning"}
    if m.link:
        headers["Click"] = m.link
    token = _secret(cfg.ntfy_token_file)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(cfg.ntfy_url, data=m.body.encode("utf-8"), headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=cfg.timeout) as r:  # noqa: S310 – configured URL
        if r.status >= 300:
            raise RuntimeError(f"ntfy answered {r.status}")


SENDERS = {"email": send_email, "ntfy": send_ntfy}


def dispatch(session: Session, cfg: NotifySettings, now: dt.datetime | None = None,
             senders: dict | None = None) -> Counter:
    """Send pending notifications. Safe to call often (e.g. after every spool drain)."""
    senders = senders or SENDERS
    now = now or dt.datetime.now()
    stats: Counter = Counter()
    rows = session.execute(
        select(Alert, AlertRule, Study).join(AlertRule, AlertRule.id == Alert.rule_id)
        .join(Study, Study.id == Alert.study_id)
        .where(Alert.notified_at.is_(None), AlertRule.notify.is_(True))
        .order_by(Alert.id).limit(200)).all()
    if not rows:
        return stats
    names = {d.id: d.display_name or d.station_name for d in session.scalars(select(Device))}
    channels = cfg.channels
    horizon = now - dt.timedelta(hours=cfg.max_age_hours)
    for a, rule, s in rows:
        # a device alert is dated when its condition was seen, a study alert by its study
        when = a.study_datetime if rule.kind in DEVICE_KINDS else s.study_datetime
        if not when or when < horizon:
            a.notified_at, a.notify_error = now, "not sent (historical)"
            stats["historical"] += 1
            continue
        if not channels:
            a.notified_at, a.notify_error = now, "not sent (no channel configured)"
            stats["no_channel"] += 1
            continue
        m = compose(a, rule, s, names.get(s.device_id), cfg.base_url)
        errors = []
        for ch in channels:
            try:
                senders[ch](cfg, m)
                stats[f"sent_{ch}"] += 1
            except Exception as exc:  # noqa: BLE001 – report and retry later
                log.warning("alert %s: %s failed: %s", a.id, ch, exc)
                errors.append(f"{ch}: {exc}")
        if errors and len(errors) == len(channels):
            prev = re.match(r"failed \(try (\d+)\)", a.notify_error or "")
            tries = (int(prev.group(1)) if prev else 0) + 1
            a.notify_error = f"failed (try {tries}): " + "; ".join(errors)
            if tries >= MAX_TRIES:
                a.notified_at = now
            stats["failed"] += 1
        else:
            a.notified_at = now
            a.notify_error = "; ".join(errors) or None
    session.flush()
    return stats
