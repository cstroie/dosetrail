"""Local diagnostic reference levels (ICRP 135): propose from local data, adopt, review.

**Proposal.** For every examination of the national DRL vocabulary and every patient group
of the chosen grid, the local values are collected per room (device), exactly as the DRL
comparison collects them (``drl.collect``: CT per series, radiography / fluoroscopy per
study, mammography per study; DRL exclusions left out). Then, as ICRP 135 describes:

* rooms with at least ``min_n`` values count;
* two or more such rooms: the local DRL is the **P75 of the room medians**;
* one room: its **median** (ICRP 135's "typical value" for a single facility);
* none: no proposal (the row shows the counts, so the gap is visible).

Grids: ``national`` (OMS 838/2025 age groups 1–4, 5–9, 10–14, ≥15 years) or ``icrp`` (the
second grid: head exams by age, body exams by weight; see grid.py). Each row shows the
national DRL (national grid) or the RP 185 EDRL (ICRP grid) where one exists.

**Adoption.** An administrator picks the rows to keep; they become a reference set with
key ``local-…``, usable wherever a reference set is (comparison page, alerts, reports).
Seeded sets are never touched; local sets can be deleted.

**Review** (Directive 2013/59 Art. 56: DRLs are reviewed regularly; typically yearly):
the adopted set against a new period — each value with the new proposal, the change, the
rooms whose median is above the local DRL, and the national / European reference.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import Code, CodeSystem, GroupBand, GroupScheme, ReferenceSet, ReferenceValue
from .drl import QUANTITY, Item, collect
from .stats import describe

NATIONAL = "ro-drl-ms838-2025"
NATIONAL_SET = "ro-ms838-2025"
NATIONAL_SCHEME = "ro-drl-age"
# modality family -> [(quantity, unit, applies_to)], as the national set measures them
QUANTITIES = {
    "CT": [("dlp", "mGy.cm", "series"), ("ctdivol", "mGy", "series")],
    "RX": [("dap", "Gy.cm2", "study")],
    "RF": [("dap", "Gy.cm2", "study")],
    "MG": [("mgd", "mGy", "study")],
}
FAMILY = {"CT": "CT", "DX": "RX", "CR": "RX", "RX": "RX", "RF": "RF", "XA": "RF", "MG": "MG"}
LOCAL_PREFIX = "local-"


class LocalDRLError(ValueError):
    pass


def _sig(v: float | None, digits: int = 3) -> float | None:
    if v is None or v == 0:
        return v
    return float(f"{v:.{digits}g}")


def _templates(session: Session, grid: str) -> list[dict]:
    """Every (exam, scheme, band, quantity) the grid can hold."""
    system = session.scalar(select(CodeSystem).filter_by(key=NATIONAL))
    if system is None:
        raise LocalDRLError("the national DRL vocabulary is not loaded (dosetrail seed)")
    codes = list(session.scalars(select(Code).filter_by(system_id=system.id)
                                 .order_by(Code.sort_order, Code.code)))
    schemes = {s.key: s for s in session.scalars(select(GroupScheme))}

    def bands(key):
        s = schemes.get(key)
        if s is None:
            raise LocalDRLError(f"group scheme {key!r} is not loaded (dosetrail seed)")
        return [(s, b) for b in sorted(s.bands, key=lambda b: b.sort_order)]

    if grid == "national":
        per_code = {c.code: bands(NATIONAL_SCHEME) for c in codes}
    elif grid == "icrp":
        from .grid import Grid, config

        g, cfg = Grid(session), config()
        per_code = {c.code: bands(cfg["age_scheme"] if g.parameter(NATIONAL, c.code) == "age"
                                  else cfg["weight_scheme"]) for c in codes}
    else:
        raise LocalDRLError("grid is national or icrp")
    out = []
    for c in codes:
        fam = FAMILY.get((c.modality or "").upper())
        for quantity, unit, applies_to in QUANTITIES.get(fam, []):
            for scheme, band in per_code[c.code]:
                out.append({"code": c, "scheme": scheme, "band": band, "quantity": quantity,
                            "unit": unit, "applies_to": applies_to})
    return out


def _references(session: Session, grid: str) -> dict[tuple, float]:
    """(exam, band, quantity) -> the national DRL (national grid) or RP 185 EDRL (icrp),
    in the local unit."""
    if grid == "icrp":
        from .reports.grid import _edrl

        return _edrl(session)
    rs = session.scalar(select(ReferenceSet).filter_by(key=NATIONAL_SET))
    if rs is None:
        return {}
    out = {}
    for rv, code, band in session.execute(
            select(ReferenceValue, Code.code, GroupBand.key)
            .join(Code, Code.id == ReferenceValue.exam_code_id)
            .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
            .where(ReferenceValue.set_id == rs.id)):
        factor = QUANTITY.get(rv.quantity, (None, {}))[1].get(rv.unit)
        if factor:
            out[(code, band, rv.quantity)] = rv.value / factor
    return out


def _room_rule(per_dev: dict, min_n: int, names: dict) -> dict:
    rooms = []
    every = []
    for dev, vals in per_dev.items():
        d = describe(vals)
        every += vals
        rooms.append({"device": dev, "device_name": names.get(dev, f"Device {dev}"),
                      "n": d["n"], "median": d["median"], "counts": d["n"] >= min_n})
    rooms.sort(key=lambda r: -r["n"])
    medians = [r["median"] for r in rooms if r["counts"]]
    if len(medians) >= 2:
        value, method = describe(medians)["p75"], "p75_rooms"
    elif medians:
        value, method = medians[0], "median_room"
    else:
        value, method = None, None
    pooled = describe(every) if every else {"n": 0, "median": None}
    return {"rooms": rooms, "n": pooled["n"], "pooled_median": pooled["median"],
            "n_rooms": len(medians), "value": _sig(value), "method": method}


def _status(value: float | None, ref: float | None) -> str | None:
    if value is None or not ref:
        return None
    r = value / ref
    return "below" if r <= 1 else "above" if r <= 1.5 else "well_above"


def propose(session: Session, conds: list, grid: str = "national", min_n: int = 10,
            estimated_weights: bool = True) -> dict:
    """Proposed local DRLs for the studies ``conds`` selects (put the period in conds)."""
    from .api.data import device_names

    templates = _templates(session, grid)
    items = [Item(key=i, exam_code_id=t["code"].id, quantity=t["quantity"], unit=t["unit"],
                  applies_to=t["applies_to"], scheme=t["scheme"].key,
                  parameter=t["scheme"].parameter, band=t["band"].key)
             for i, t in enumerate(templates)]
    got = collect(session, conds, items, by_device=True, estimated_weights=estimated_weights)
    refs = _references(session, grid)
    names = device_names(session)
    rows = []
    for i, t in enumerate(templates):
        per_dev = got.get(i) or {}
        if not per_dev:
            continue
        rule = _room_rule(per_dev, min_n, names)
        ref = refs.get((t["code"].code, t["band"].key, t["quantity"]))
        rows.append({
            "exam": t["code"].code, "exam_meaning": t["code"].meaning,
            "modality": t["code"].modality, "scheme": t["scheme"].key,
            "band": t["band"].key, "band_label": t["band"].label,
            "quantity": t["quantity"], "unit": t["unit"], "applies_to": t["applies_to"],
            **rule, "reference": _sig(ref),
            "ratio": rule["value"] / ref if rule["value"] and ref else None,
            "status": _status(rule["value"], ref)})
    return {"grid": grid, "min_n": min_n, "estimated_weights": estimated_weights,
            "reference_kind":
            "national DRL (OMS 838/2025)" if grid == "national" else "EDRL (RP 185)",
            "rows": rows}


def adopt(session: Session, rows: list[dict], *, name: str, grid: str, start: dt.date,
          end: dt.date, min_n: int, query: str, now: dt.datetime | None = None
          ) -> ReferenceSet:
    """A new local reference set from the chosen proposal rows (value may be edited)."""
    now = now or dt.datetime.now()
    name = (name or "").strip()
    if not name:
        raise LocalDRLError("a name is required")
    if not rows:
        raise LocalDRLError("choose at least one value")
    system = session.scalar(select(CodeSystem).filter_by(key=NATIONAL))
    codes = {c.code: c for c in session.scalars(select(Code).filter_by(system_id=system.id))}
    bands = {(s.key, b.key): b for s in session.scalars(select(GroupScheme)) for b in s.bands}
    key = f"{LOCAL_PREFIX}{grid}-{now:%Y%m%d-%H%M%S}"
    rs = ReferenceSet(
        key=key, name=name[:255], valid_from=now.date(),
        source=f"Local DRL (DoseTrail): {start.isoformat()} – {end.isoformat()}, "
               f"{'national age groups' if grid == 'national' else 'ICRP 135 / RP 185 groups'}"
               f", query {query or '(all)'!s}"[:512],
        notes=f"P75 of the room medians (rooms with ≥ {min_n} values); a single room: its "
              "median. CT per series; radiography and fluoroscopy per study.")
    session.add(rs)
    for r in rows:
        code = codes.get(r.get("exam"))
        band = bands.get((r.get("scheme"), r.get("band")))
        q = r.get("quantity")
        if code is None or band is None or q not in QUANTITY:
            raise LocalDRLError(f"unknown exam / group / quantity: {r.get('exam')} "
                                f"{r.get('band')} {q}")
        try:
            value = float(r["value"])
        except (KeyError, TypeError, ValueError):
            raise LocalDRLError(f"{code.code} {band.key} {q}: no value") from None
        if value <= 0:
            raise LocalDRLError(f"{code.code} {band.key} {q}: the value must be positive")
        if r.get("unit") not in QUANTITY[q][1]:
            raise LocalDRLError(f"{code.code} {q}: unit {r.get('unit')!r} not supported")
        rs.values.append(ReferenceValue(exam_code=code, band=band, quantity=q, value=value,
                                        unit=r["unit"],
                                        applies_to=r.get("applies_to") or "study"))
    session.flush()
    return rs


def delete(session: Session, key: str) -> dict:
    if not key.startswith(LOCAL_PREFIX):
        raise LocalDRLError("only local reference sets can be deleted (the others are "
                            "bundled and reloaded by dosetrail seed)")
    rs = session.scalar(select(ReferenceSet).filter_by(key=key))
    if rs is None:
        raise LocalDRLError(f"unknown reference set {key!r}")
    out = {"key": rs.key, "name": rs.name, "values": len(rs.values)}
    session.delete(rs)
    session.flush()
    return out


def review(session: Session, set_key: str, conds: list, min_n: int = 10) -> dict:
    """An adopted local set against the period ``conds`` selects."""
    from .api.data import device_names

    rs = session.scalar(select(ReferenceSet).filter_by(key=set_key))
    if rs is None:
        raise LocalDRLError(f"unknown reference set {set_key!r}")
    m = re.match(rf"{LOCAL_PREFIX}(national|icrp)-", rs.key)
    grid = m.group(1) if m else "national"
    vals = session.execute(
        select(ReferenceValue, Code, GroupBand, GroupScheme)
        .join(Code, Code.id == ReferenceValue.exam_code_id)
        .outerjoin(GroupBand, GroupBand.id == ReferenceValue.band_id)
        .outerjoin(GroupScheme, GroupScheme.id == GroupBand.scheme_id)
        .where(ReferenceValue.set_id == rs.id)
        .order_by(Code.sort_order, Code.code, GroupBand.sort_order, ReferenceValue.quantity)
    ).all()
    items = [Item(key=i, exam_code_id=rv.exam_code_id, quantity=rv.quantity, unit=rv.unit,
                  applies_to=rv.applies_to, scheme=sc.key if sc else None,
                  parameter=sc.parameter if sc else None, band=b.key if b else None)
             for i, (rv, _c, b, sc) in enumerate(vals)]
    got = collect(session, conds, items, by_device=True)
    refs = _references(session, grid)
    names = device_names(session)
    rows = []
    for i, (rv, code, band, _sc) in enumerate(vals):
        rule = _room_rule(got.get(i) or {}, min_n, names)
        # collect() returns values in the set's unit; references are in the local unit
        factor = QUANTITY[rv.quantity][1].get(rv.unit, 1.0)
        local = rv.value
        over = [r for r in rule["rooms"] if r["counts"] and r["median"] > local]
        new = rule["value"]
        ref = refs.get((code.code, band.key if band else None, rv.quantity))
        ref = ref * factor if ref else None
        rows.append({
            "exam": code.code, "exam_meaning": code.meaning,
            "band": band.key if band else None, "band_label": band.label if band else None,
            "quantity": rv.quantity, "unit": rv.unit, "local_drl": rv.value,
            "new_value": new,
            "change_pct": round((new / local - 1) * 100, 1) if new is not None and local
            else None,
            "n": rule["n"], "n_rooms": rule["n_rooms"], "method": rule["method"],
            "rooms_above": [{"device_name": r["device_name"], "n": r["n"],
                             "median": _sig(r["median"])} for r in over],
            "reference": _sig(ref),
            "status": _status(new, ref)})
    return {"set": rs.key, "name": rs.name, "source": rs.source,
            "valid_from": rs.valid_from.isoformat() if rs.valid_from else None,
            "grid": grid, "min_n": min_n, "rows": rows}


def write_review_xlsx(rep: dict, target: IO, info: dict[str, object]) -> None:
    from .export.xlsx import XlsxWriter

    status = {"below": "≤ referință", "above": "≤ 1,5 × referință",
              "well_above": "> 1,5 × referință", None: ""}
    with XlsxWriter(target) as wb:
        about = wb.sheet("Informatii", ["Câmp", "Valoare"], [30, 90])
        for k, v in [("Raport", "Revizuirea nivelurilor de referință locale (NRD locale)"),
                     ("Set", f"{rep['name']} ({rep['set']})"), ("Adoptat", rep["valid_from"]),
                     ("Sursa", rep["source"]), *info.items(),
                     ("Metodă", f"P75 al medianelor pe sală (săli cu ≥ {rep['min_n']} "
                                "valori); o singură sală: mediana ei"),
                     ("Referință", "NRD național (OMS 838/2025)" if rep["grid"] == "national"
                      else "EDRL (RP 185)")]:
            about.row([k, v])
        ws = wb.sheet("Revizuire", ["Examinare", "Cod", "Grupa", "Mărime", "Unitate",
                                    "NRD local", "Valoare nouă", "Schimbare %", "n",
                                    "Săli", "Săli peste NRD local", "Referință", "Stare"],
                      [30, 16, 14, 9, 9, 10, 11, 10, 7, 6, 40, 10, 16])
        for r in rep["rows"]:
            ws.row([r["exam_meaning"], r["exam"], r["band_label"], r["quantity"], r["unit"],
                    r["local_drl"], r["new_value"], r["change_pct"], r["n"], r["n_rooms"],
                    "; ".join(f"{x['device_name']} ({x['median']}, n {x['n']})"
                              for x in r["rooms_above"]) or None,
                    r["reference"], status[r["status"]]])
