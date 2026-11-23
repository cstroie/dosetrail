"""Small secrets stored in the database (e.g. a HIS password), encrypted at rest.

Fernet (AES-128-CBC + HMAC-SHA256) keyed by an HKDF subkey of the site secret file, so a
database dump alone does not reveal them. Changing the site secret makes stored secrets
unreadable: they must then be typed in again.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"dosimetrix:secretbox:v1"


class SecretError(ValueError):
    pass


def _fernet(site_secret: bytes) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(site_secret)
    return Fernet(base64.urlsafe_b64encode(key))


def seal(site_secret: bytes, plaintext: str) -> str:
    return _fernet(site_secret).encrypt(plaintext.encode()).decode("ascii")


def open_(site_secret: bytes, token: str) -> str:
    try:
        return _fernet(site_secret).decrypt(token.encode("ascii")).decode()
    except (InvalidToken, ValueError):
        raise SecretError("stored secret cannot be decrypted (was the site secret changed?): "
                          "enter it again") from None
