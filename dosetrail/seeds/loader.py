"""Load bundled seed data (idempotent: re-running updates in place, never duplicates)."""

from __future__ import annotations

import csv
import json
import tomllib
from importlib import resources
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import (
    Code,
    CodeSystem,
    GroupBand,
    GroupScheme,
    ReferenceSet,
    ReferenceValue,
    UnitConversion,
)

SEEDS = Path(str(resources.files(__package__)))


def _toml(name: str) -> dict:
    with (SEEDS / name).open("rb") as fh:
        return tomllib.load(fh)


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _get_or_new(session: Session, model, **key):
    obj = session.scalar(select(model).filter_by(**key))
    if obj is None:
        obj = model(**key)
        session.add(obj)
    return obj


def load_code_systems(session: Session) -> list[str]:
    report = []
    for meta in _toml("systems.toml")["system"]:
        cs = _get_or_new(session, CodeSystem, key=meta["key"])
        for f in ("name", "version", "source", "author", "licence"):
            setattr(cs, f, meta.get(f))
        session.flush()
        existing = {c.code: c for c in session.scalars(select(Code).filter_by(system_id=cs.id))}
        rows = _csv(SEEDS / "codes" / f"{meta['key']}.csv")
        for i, r in enumerate(rows):
            c = existing.get(r["code"]) or Code(system_id=cs.id, code=r["code"])
            c.meaning = r["meaning"]
            c.chapter = r["chapter"] or None
            c.modality = r["modality"] or None
            c.attributes = json.loads(r["attributes"]) if r["attributes"] else None
            c.sort_order = i
            session.add(c)
        report.append(f"code system {meta['key']}: {len(rows)} codes")
    session.flush()
    return report


def load_group_schemes(session: Session) -> list[str]:
    report = []
    for meta in _toml("group_schemes.toml")["scheme"]:
        gs = _get_or_new(session, GroupScheme, key=meta["key"])
        gs.name, gs.parameter, gs.source = meta["name"], meta["parameter"], meta.get("source")
        session.flush()
        existing = {b.key: b for b in gs.bands}
        for i, b in enumerate(meta["bands"]):
            band = existing.get(b["key"]) or GroupBand(scheme_id=gs.id, key=b["key"])
            band.label = b["label"]
            band.lower, band.lower_unit = b.get("lower"), b.get("lower_unit")
            band.upper, band.upper_unit = b.get("upper"), b.get("upper_unit")
            band.sort_order = i
            if band not in gs.bands:
                gs.bands.append(band)
        report.append(f"group scheme {meta['key']}: {len(meta['bands'])} bands")
    session.flush()
    return report


def load_reference_sets(session: Session) -> list[str]:
    report = []
    bands = {
        (s.key, b.key): b
        for s in session.scalars(select(GroupScheme))
        for b in s.bands
    }
    for meta in _toml("reference_sets.toml")["set"]:
        system = session.scalar(select(CodeSystem).filter_by(key=meta["exam_system"]))
        if system is None:
            raise ValueError(f"reference set {meta['key']}: unknown system {meta['exam_system']}")
        codes = {c.code: c for c in session.scalars(select(Code).filter_by(system_id=system.id))}
        rs = _get_or_new(session, ReferenceSet, key=meta["key"])
        rs.name, rs.source, rs.notes = meta["name"], meta.get("source"), meta.get("notes")
        rs.values.clear()  # values are fully owned by the seed file
        session.flush()
        rows = _csv(SEEDS / "drl" / f"{meta['key']}.csv")
        for r in rows:
            if r["exam"] not in codes:
                raise ValueError(f"{meta['key']}: unknown exam code {r['exam']}")
            band = None
            if r["band"]:
                band = bands.get((r["scheme"], r["band"]))
                if band is None:
                    raise ValueError(f"{meta['key']}: unknown band {r['scheme']}/{r['band']}")
            rs.values.append(ReferenceValue(
                exam_code=codes[r["exam"]], band=band, quantity=r["quantity"],
                value=float(r["value"]), unit=r["unit"], applies_to=r["applies_to"],
            ))
        report.append(f"reference set {meta['key']}: {len(rows)} values")
    session.flush()
    return report


def load_unit_conversions(session: Session) -> list[str]:
    rows = _csv(SEEDS / "unit_conversions.csv")
    for r in rows:
        uc = _get_or_new(session, UnitConversion, quantity=r["quantity"], from_unit=r["from_unit"])
        uc.to_unit, uc.factor = r["to_unit"], float(r["factor"])
    session.flush()
    return [f"unit conversions: {len(rows)}"]


def load_all(session: Session) -> list[str]:
    return (
        load_code_systems(session)
        + load_group_schemes(session)
        + load_reference_sets(session)
        + load_unit_conversions(session)
    )
