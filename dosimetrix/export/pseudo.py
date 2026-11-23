"""Keyed pseudonyms and coarse ages shared by the pseudonymised exports (DRL dataset, survey).

Codes are HMAC-SHA256 with the site secret: equal codes mean the same thing, and they cannot be
reversed without the secret.
"""

from __future__ import annotations

import hashlib
import hmac

NATIONAL, EUROPEAN = "ro-drl-ms838-2025", "ec-rp185"
NOMENCLATORS = ("ro-nom-ct", "ro-nom-rx")


def _code(secret: bytes, kind: str, value) -> str:
    mac = hmac.new(secret, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"{kind[0].upper()}-{mac[:8].upper()}"


def coarse_age(days: int | None) -> dict:
    """Age as the survey plan asks: days below 1 year, months up to 24 months, completed
    years from 2 years on. Finer values are left out (with month, sex and device they would
    narrow down the birth date)."""
    if days is None:
        return {"age_days": None, "age_months": None, "age_years": None}
    months = int(days / 30.4375)
    return {"age_days": days if days < 365 else None,
            "age_months": months if months < 24 else None,
            "age_years": int(days / 365.25)}


def patient_code(secret: bytes, p) -> str | None:
    """Stable across re-imports: keyed on the patient hash, not the database id."""
    if p is None:
        return None
    return _code(secret, "patient", p.legacy_sha256 or f"id:{p.id}")


def device_code(secret: bytes, d) -> str | None:
    return _code(secret, "device", d.identity_key or f"id:{d.id}") if d is not None else None


def _r(x, nd=6):
    return round(x, nd) if isinstance(x, float) else x
