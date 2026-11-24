"""Portable column types with MariaDB-specific variants."""

from __future__ import annotations

from sqlalchemy import JSON, String
from sqlalchemy.dialects import mysql

_ASCII_BIN = dict(charset="ascii", collation="ascii_bin")


def AsciiId(length: int = 64):  # noqa: N802 - type factory
    """Exact-match ASCII identifier (DICOM UIDs, serials, hashes, codes).

    On MariaDB: VARCHAR(n) CHARACTER SET ascii COLLATE ascii_bin (compact, case-sensitive).
    Elsewhere: plain VARCHAR(n).
    """
    return (
        String(length)
        .with_variant(mysql.VARCHAR(length, **_ASCII_BIN), "mysql")
        .with_variant(mysql.VARCHAR(length, **_ASCII_BIN), "mariadb")
    )


def Text(length: int = 255):  # noqa: N802
    """Human text; on MariaDB uses the database default (utf8mb4_unicode_ci, case-insensitive)."""
    return String(length)


JSONType = JSON
