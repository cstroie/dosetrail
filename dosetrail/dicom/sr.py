"""A small, dependency-free view of a DICOM SR content tree.

Concept names are matched by code value only (DCM codes are stable); coded *values* may come
from different schemes (SRT 'P5-08000' vs SNOMED '77477000'), so callers compare them with
`code_in()` using both code values and meanings.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

from .units import convert


@dataclass
class Node:
    code: str  # concept name code value ('' for unnamed)
    meaning: str
    value_type: str
    value: object = None  # TEXT/UIDREF/DATETIME/PNAME: str; CODE: (value, scheme, meaning)
    number: float | None = None
    unit: str | None = None
    children: list[Node] = field(default_factory=list)

    # ------------------------------------------------------------------ navigation

    def iter(self) -> Iterator[Node]:
        for c in self.children:
            yield c
            yield from c.iter()

    def find(self, code: str, deep: bool = True) -> Node | None:
        for n in (self.iter() if deep else self.children):
            if n.code == code:
                return n
        return None

    def find_all(self, code: str, deep: bool = False) -> list[Node]:
        return [n for n in (self.iter() if deep else self.children) if n.code == code]

    # ------------------------------------------------------------------ values

    def num(self, code: str, target: str | None = None, deep: bool = True) -> float | None:
        n = self.find(code, deep)
        if n is None or n.number is None:
            return None
        return n.number if target is None else convert(n.number, n.unit, target)

    def text(self, code: str, deep: bool = True) -> str | None:
        n = self.find(code, deep)
        if n is None:
            return None
        if n.value_type == "CODE" and n.value:
            return n.value[2]
        return str(n.value).strip() if n.value is not None else None

    def coded(self, code: str, deep: bool = True) -> tuple[str, str, str] | None:
        n = self.find(code, deep)
        return n.value if n is not None and n.value_type == "CODE" else None

    def datetime(self, code: str, deep: bool = True) -> dt.datetime | None:
        n = self.find(code, deep)
        return parse_dt(n.value) if n is not None and n.value else None


def code_in(coded: tuple[str, str, str] | None, values: set[str], meanings: set[str]) -> bool:
    if not coded:
        return False
    return coded[0] in values or (coded[2] or "").strip().lower() in meanings


def parse_dt(value: str | None) -> dt.datetime | None:
    """DICOM DT ('YYYYMMDDHHMMSS.FFFFFF&ZZXX') -> naive datetime (zone offset dropped)."""
    if not value:
        return None
    v = str(value).strip()
    for sep in ("+", "-"):
        i = v.find(sep, 8)
        if i > 0:
            v = v[:i]
    main, _, frac = v.partition(".")
    try:
        base = dt.datetime.strptime(main.ljust(14, "0")[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if frac.isdigit():
        base = base.replace(microsecond=int(frac.ljust(6, "0")[:6]))
    return base


def tree(ds) -> Node:
    """Build the content tree of an SR dataset (root = document title)."""
    cn = ds.ConceptNameCodeSequence[0] if ds.get("ConceptNameCodeSequence") else None
    root = Node(code=str(cn.CodeValue) if cn else "", meaning=str(cn.CodeMeaning) if cn else "",
                value_type="CONTAINER")
    root.children = [_node(it) for it in ds.get("ContentSequence", [])]
    return root


def _node(it) -> Node:
    cn = it.ConceptNameCodeSequence[0] if it.get("ConceptNameCodeSequence") else None
    vt = str(it.get("ValueType", ""))
    n = Node(code=str(cn.CodeValue) if cn else "", meaning=str(cn.CodeMeaning) if cn else "",
             value_type=vt)
    if vt == "NUM" and it.get("MeasuredValueSequence"):
        mv = it.MeasuredValueSequence[0]
        try:
            n.number = float(mv.NumericValue)
        except (TypeError, ValueError):
            n.number = None
        units = mv.get("MeasurementUnitsCodeSequence")
        n.unit = str(units[0].CodeValue) if units else None
    elif vt == "CODE" and it.get("ConceptCodeSequence"):
        c = it.ConceptCodeSequence[0]
        n.value = (str(c.get("CodeValue", "")), str(c.get("CodingSchemeDesignator", "")),
                   str(c.get("CodeMeaning", "")))
    elif vt == "TEXT":
        n.value = it.get("TextValue")
    elif vt == "UIDREF":
        n.value = it.get("UID")
    elif vt == "DATETIME":
        n.value = it.get("DateTime")
    elif vt == "PNAME":
        n.value = str(it.get("PersonName", "")) or None
    n.children = [_node(c) for c in it.get("ContentSequence", [])]
    return n
