"""Administrative writes on devices. Every change is audit-logged by the caller.

Model: a *record* is one equipment identity seen in the data (manufacturer, model, serial,
station…). A *physical device* is a record that is not merged into another; records merged
into it are its aliases (`merged_into_id`). Display name, modality override and origin
belong to the physical device and are applied to all its aliases.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import AppSetting, Device

DISMISSED_KEY = "devices.merge_suggestions.dismissed"
MODALITIES = {"CT", "DX", "CR", "RF", "XA", "MG", "NM", "PT"}
HIS_POLICIES = ("full", "patient", "none")  # HIS lookups: patient + request | patient | off


class AdminError(ValueError):
    pass


def _physical(session: Session, device_id: int) -> Device:
    d = session.get(Device, device_id)
    if d is None:
        raise AdminError("device not found")
    while d.merged_into_id:
        d = session.get(Device, d.merged_into_id)
    return d


def _group(session: Session, phys: Device) -> list[Device]:
    return [phys, *session.scalars(select(Device).where(Device.merged_into_id == phys.id))]


def update_device(session: Session, device_id: int, patch: dict) -> dict:
    phys = _physical(session, device_id)
    group = _group(session, phys)
    changes: dict = {}
    if "display_name" in patch:
        name = (patch["display_name"] or "").strip()
        if not name or len(name) > 128:
            raise AdminError("display name must have 1–128 characters")
        changes["display_name"] = (phys.display_name, name)
        phys.display_name = name
    if "modality_override" in patch:
        mod = patch["modality_override"]
        mod = None if mod in (None, "", "auto") else str(mod).upper()
        if mod is not None and mod not in MODALITIES:
            raise AdminError(f"unknown modality {mod}")
        changes["modality_override"] = (phys.modality_override, mod)
        for d in group:
            d.modality_override = mod
    if "origin" in patch:
        if patch["origin"] not in ("local", "external"):
            raise AdminError("origin must be 'local' or 'external'")
        local = patch["origin"] == "local"
        changes["origin"] = ("local" if phys.is_local else "external", patch["origin"])
        for d in group:
            d.is_local = local
    if "his_policy" in patch:
        if patch["his_policy"] not in HIS_POLICIES:
            raise AdminError(f"HIS policy is one of: {', '.join(HIS_POLICIES)}")
        changes["his_policy"] = (phys.his_policy, patch["his_policy"])
        for d in group:
            d.his_policy = patch["his_policy"]
    if not changes:
        raise AdminError("nothing to change")
    session.flush()
    return {"device_id": phys.id, "changes": {k: {"from": a, "to": b}
                                              for k, (a, b) in changes.items()}}


def merge_devices(session: Session, record_ids: list[int], survivor_id: int,
                  display_name: str | None = None) -> dict:
    ids = list(dict.fromkeys(record_ids))
    if survivor_id not in ids or len(ids) < 2:
        raise AdminError("give at least two records, including the survivor")
    survivor = _physical(session, survivor_id)
    if survivor.id != survivor_id:
        raise AdminError("the survivor is itself merged into another device")
    moved = []
    for rid in ids:
        if rid == survivor.id:
            continue
        phys = _physical(session, rid)
        if phys.id == survivor.id:
            continue  # already merged
        for d in _group(session, phys):  # move the whole group, keep the tree flat
            d.merged_into_id = survivor.id
            d.is_local = survivor.is_local
            d.modality_override = survivor.modality_override
            d.his_policy = survivor.his_policy
            moved.append(d.id)
    if display_name:
        survivor.display_name = display_name.strip()[:128]
    session.flush()
    return {"survivor_id": survivor.id, "merged": moved,
            "display_name": survivor.display_name}


def split_device(session: Session, device_id: int) -> dict:
    d = session.get(Device, device_id)
    if d is None:
        raise AdminError("device not found")
    if not d.merged_into_id:
        raise AdminError("this record is not merged")
    former = d.merged_into_id
    d.merged_into_id = None
    session.flush()
    return {"device_id": d.id, "split_from": former}


def dismissed(session: Session) -> set[tuple[int, ...]]:
    row = session.get(AppSetting, DISMISSED_KEY)
    return {tuple(sorted(x)) for x in (row.value or [])} if row else set()


def dismiss_suggestion(session: Session, record_ids: list[int]) -> dict:
    key = tuple(sorted(set(record_ids)))
    if len(key) < 2:
        raise AdminError("a suggestion has at least two records")
    row = session.get(AppSetting, DISMISSED_KEY) or AppSetting(key=DISMISSED_KEY, value=[])
    current = {tuple(x) for x in (row.value or [])}
    current.add(key)
    row.value = sorted(list(x) for x in current)
    session.add(row)
    session.flush()
    return {"dismissed": list(key)}
