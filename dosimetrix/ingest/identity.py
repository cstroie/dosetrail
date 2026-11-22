"""Stable identity keys for devices and patients (shared by every ingest path)."""

from __future__ import annotations

import hashlib


def device_key(manufacturer=None, model=None, serial=None, station=None, institution=None,
               department=None, observer_uid=None) -> str:
    """Identity: device observer UID > manufacturer + serial > normalised tuple.
    Returns the sha256 used as Device.identity_key."""
    def clean(v):
        return (v or "").strip()

    if clean(observer_uid):
        raw = "uid:" + clean(observer_uid)
    elif clean(serial):
        raw = f"sn:{clean(manufacturer).casefold()}|{clean(serial).casefold()}"
    else:
        raw = "tuple:" + "|".join(clean(p).casefold() for p in
                                  (manufacturer, model, station, institution, department))
    return hashlib.sha256(raw.encode()).hexdigest()


def patient_key(patient_id: str) -> str:
    """OpenREM-compatible: sha256 of the UTF-8 patient ID (links legacy hashed records)."""
    return hashlib.sha256(patient_id.encode("utf-8")).hexdigest()
