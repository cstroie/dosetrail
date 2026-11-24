"""Passwords (scrypt), signed session tokens (HMAC-SHA256) and patient pseudonyms.

Stdlib only. The site secret is a random 32-byte key kept in a file outside the database
(mode 0600). Losing it changes every pseudonym and logs everyone out; it is never derived
from anything stored in the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


# ----------------------------------------------------------------------------- secret


def load_or_create_secret(path: Path, create: bool = True) -> bytes:
    if path.exists():
        data = path.read_bytes().strip()
        key = base64.b64decode(data)
        if len(key) < 32:
            raise ValueError(f"secret in {path} is too short")
        return key
    if not create:
        raise FileNotFoundError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64encode(key) + b"\n")
    return key


# -------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${dk}".format(
        n=_SCRYPT["n"], r=_SCRYPT["r"], p=_SCRYPT["p"],
        salt=base64.b64encode(salt).decode(), dk=base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                             r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- sessions


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign_session(secret: bytes, user_id: int, hours: float, now: float | None = None) -> str:
    payload = {"uid": user_id, "exp": int((now or time.time()) + hours * 3600),
               "n": secrets.token_hex(4)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = hmac.new(secret, b"session:" + body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def read_session(secret: bytes, token: str | None, now: float | None = None) -> int | None:
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    expected = hmac.new(secret, b"session:" + body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(mac), expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if payload.get("exp", 0) < (now or time.time()):
        return None
    return int(payload["uid"])


# ------------------------------------------------------------------------- pseudonyms

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


def pseudonym(secret: bytes, stable_key: str) -> str:
    """'P-XXXXX-XXXXX' (50 bits). stable_key: the patient's legacy sha256 or internal key."""
    digest = hmac.new(secret, b"patient:" + stable_key.encode(), hashlib.sha256).digest()
    n = int.from_bytes(digest[:7], "big") >> 6  # 50 bits
    chars = []
    for _ in range(10):
        chars.append(_ALPHABET[n & 31])
        n >>= 5
    code = "".join(reversed(chars))
    return f"P-{code[:5]}-{code[5:]}"
