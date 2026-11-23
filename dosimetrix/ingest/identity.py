"""Stable identity keys for devices and patients (shared by every ingest path)."""

from __future__ import annotations

import hashlib


# Serial numbers some vendors write when they have none (Toshiba: SERIALNO). Treating them
# as a real serial would merge every such scanner, in every hospital, into one device.
PLACEHOLDER_SERIALS = {"serialno", "serial", "unknown", "none", "n/a", "na", "null", "-"}


def real_serial(serial: str | None) -> str:
    """The serial number, or "" when it is empty, all zeros or a known placeholder."""
    s = (serial or "").strip()
    if not s or s.casefold() in PLACEHOLDER_SERIALS or not s.strip("0"):
        return ""
    return s


def device_key(manufacturer=None, model=None, serial=None, station=None, institution=None,
               department=None, observer_uid=None) -> str:
    """Identity: device observer UID > manufacturer + serial > normalised tuple.
    Returns the sha256 used as Device.identity_key."""
    def clean(v):
        return (v or "").strip()

    if clean(observer_uid):
        raw = "uid:" + clean(observer_uid)
    elif real_serial(serial):
        raw = f"sn:{clean(manufacturer).casefold()}|{real_serial(serial).casefold()}"
    else:
        raw = "tuple:" + "|".join(clean(p).casefold() for p in
                                  (manufacturer, model, station, institution, department))
    return hashlib.sha256(raw.encode()).hexdigest()


def patient_key(patient_id: str) -> str:
    """OpenREM-compatible: sha256 of the UTF-8 patient ID (links legacy hashed records)."""
    return hashlib.sha256(patient_id.encode("utf-8")).hexdigest()
