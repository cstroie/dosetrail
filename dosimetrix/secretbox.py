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
_FORMER = b"dosetrail:secretbox:v1"  # secrets sealed under the former name: read, then resealed


class SecretError(ValueError):
    pass


def _fernet(site_secret: bytes, info: bytes = _INFO) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(site_secret)
    return Fernet(base64.urlsafe_b64encode(key))


def seal(site_secret: bytes, plaintext: str) -> str:
    return _fernet(site_secret).encrypt(plaintext.encode()).decode("ascii")


def open_(site_secret: bytes, token: str) -> str:
    for info in (_INFO, _FORMER):
        try:
            return _fernet(site_secret, info).decrypt(token.encode("ascii")).decode()
        except (InvalidToken, ValueError):
            continue
    raise SecretError("stored secret cannot be decrypted (was the site secret changed?): "
                      "enter it again")


def is_current(site_secret: bytes, token: str) -> bool:
    """Sealed with the current key (not the former name's)."""
    try:
        _fernet(site_secret).decrypt(token.encode("ascii"))
        return True
    except (InvalidToken, ValueError):
        return False


def reseal(site_secret: bytes, token: str) -> str:
    """The same secret sealed with the current key."""
    return seal(site_secret, open_(site_secret, token))

