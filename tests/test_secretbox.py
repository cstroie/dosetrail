"""Tests for secretbox.py: encrypted secrets in DB."""

from __future__ import annotations

import pytest

from dozimetrix.secretbox import SecretError, open_, seal


class TestSecretBox:
    def setup_method(self):
        self.secret = b"x" * 32

    def test_seal_open_roundtrip(self):
        plaintext = "super-secret-password-123"
        sealed = seal(self.secret, plaintext)
        assert sealed != plaintext
        assert isinstance(sealed, str)
        assert len(sealed) > 0
        opened = open_(self.secret, sealed)
        assert opened == plaintext

    def test_empty_string(self):
        sealed = seal(self.secret, "")
        assert open_(self.secret, sealed) == ""

    def test_unicode(self):
        plaintext = "pässwörd-日本語-🔐"
        sealed = seal(self.secret, plaintext)
        assert open_(self.secret, sealed) == plaintext

    def test_different_secrets_produce_different_ciphertext(self):
        other = b"y" * 32
        sealed1 = seal(self.secret, "same")
        sealed2 = seal(other, "same")
        assert sealed1 != sealed2

    def test_wrong_secret_raises(self):
        other = b"y" * 32
        sealed = seal(self.secret, "test")
        with pytest.raises(SecretError):
            open_(other, sealed)

    def test_tampered_ciphertext_raises(self):
        sealed = seal(self.secret, "test")
        # Flip a bit in the base64
        tampered = sealed[:-1] + ("a" if sealed[-1] != "a" else "b")
        with pytest.raises(SecretError):
            open_(self.secret, tampered)

    def test_truncated_ciphertext_raises(self):
        sealed = seal(self.secret, "test")
        with pytest.raises(SecretError):
            open_(self.secret, sealed[:-5])

    def test_invalid_base64_raises(self):
        with pytest.raises(SecretError):
            open_(self.secret, "not-base64!!!")

    def test_same_plaintext_different_ciphertext_each_time(self):
        # Fernet uses random IV, so same plaintext -> different ciphertext
        c1 = seal(self.secret, "same")
        c2 = seal(self.secret, "same")
        assert c1 != c2
        assert open_(self.secret, c1) == open_(self.secret, c2) == "same"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
