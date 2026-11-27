"""Tests for security.py: passwords, sessions, pseudonyms."""

from __future__ import annotations

import pytest

from dozimetrix.security import (
    _b64,
    _unb64,
    hash_password,
    load_or_create_secret,
    pseudonym,
    read_session,
    read_session_claims,
    session_stamp,
    sign_session,
    verify_password,
)


class TestPasswords:
    def test_hash_verify_roundtrip(self):
        pw = "correct-horse-battery-staple"
        h = hash_password(pw)
        assert h.startswith("scrypt$")
        assert verify_password(pw, h)
        assert not verify_password("wrong", h)

    def test_different_salts_produce_different_hashes(self):
        pw = "same"
        h1 = hash_password(pw)
        h2 = hash_password(pw)
        assert h1 != h2  # different salts
        assert verify_password(pw, h1)
        assert verify_password(pw, h2)

    def test_scrypt_params_embedded(self):
        h = hash_password("x")
        parts = h.split("$")
        assert parts[0] == "scrypt"
        assert parts[1] == "16384"  # 2**14
        assert parts[2] == "8"
        assert parts[3] == "1"


class TestSessions:
    def setup_method(self):
        self.secret = b"x" * 32

    def test_sign_read_roundtrip(self):
        token = sign_session(self.secret, 42, 1.0)
        uid = read_session(self.secret, token)
        assert uid == 42

    def test_expired_token_rejected(self):
        token = sign_session(self.secret, 99, -1.0)  # expired 1 hour ago
        assert read_session(self.secret, token) is None

    def test_tampered_token_rejected(self):
        token = sign_session(self.secret, 1, 1.0)
        body, mac = token.rsplit(".", 1)
        tampered = body + "." + mac[:-1] + ("a" if mac[-1] != "a" else "b")
        assert read_session(self.secret, tampered) is None

    def test_wrong_secret_rejected(self):
        token = sign_session(self.secret, 7, 1.0)
        other = b"y" * 32
        assert read_session(other, token) is None

    def test_token_too_long_rejected(self):
        assert read_session(self.secret, "x" * 600) is None

    def test_malformed_token_rejected(self):
        assert read_session(self.secret, "no.dot.here") is None
        assert read_session(self.secret, "notbase64!.notbase64!") is None

    def test_stamp_changes_with_password(self):
        stamp1 = session_stamp(self.secret, "hash1", "admin")
        stamp2 = session_stamp(self.secret, "hash2", "admin")
        assert stamp1 != stamp2

    def test_stamp_changes_with_roles(self):
        stamp1 = session_stamp(self.secret, "hash", "admin")
        stamp2 = session_stamp(self.secret, "hash", "viewer")
        assert stamp1 != stamp2

    def test_stamp_changes_with_epoch(self):
        stamp1 = session_stamp(self.secret, "hash", "admin", 0)
        stamp2 = session_stamp(self.secret, "hash", "admin", 1)
        assert stamp1 != stamp2

    def test_read_session_claims_returns_stamp(self):
        token = sign_session(self.secret, 1, 1.0, stamp="abc123")
        claims = read_session_claims(self.secret, token)
        assert claims == (1, "abc123")


class TestPseudonyms:
    def setup_method(self):
        self.secret = b"x" * 32

    def test_deterministic_per_secret(self):
        p1 = pseudonym(self.secret, "PAT-123")
        p2 = pseudonym(self.secret, "PAT-123")
        assert p1 == p2

    def test_different_inputs_different_output(self):
        p1 = pseudonym(self.secret, "PAT-123")
        p2 = pseudonym(self.secret, "PAT-456")
        assert p1 != p2

    def test_different_secret_different_output(self):
        other = b"y" * 32
        p1 = pseudonym(self.secret, "PAT-123")
        p2 = pseudonym(other, "PAT-123")
        assert p1 != p2

    def test_alphabet_crockford_base32(self):
        # No I, L, O, U
        for _ in range(100):
            p = pseudonym(self.secret, f"PAT-{_}")
            for ch in "ILOU":
                assert ch not in p


class TestSecretLoading:
    def test_load_or_create_creates_new(self, tmp_path):
        path = tmp_path / "secret"
        key = load_or_create_secret(path)
        assert len(key) == 32
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600

    def test_load_or_create_loads_existing(self, tmp_path):
        path = tmp_path / "secret"
        original = load_or_create_secret(path)
        loaded = load_or_create_secret(path)
        assert loaded == original


class TestBase64:
    def test_roundtrip(self):
        for data in [b"", b"a", b"hello", b"x" * 100]:
            enc = _b64(data)
            dec = _unb64(enc)
            assert dec == data

    def test_urlsafe_no_padding(self):
        # Should not contain +, /, or =
        enc = _b64(b"test")
        assert "+" not in enc
        assert "/" not in enc
        assert "=" not in enc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
