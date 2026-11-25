"""Server side of the typed query language (mirror of web/assets/js/core/query.js).

    modality:CT date:2026-09-01..2026-11-21 device:"CT 1","CT 2" age:>=18 dlp:>700 -phantom

key:value · comparisons > >= < <= · ranges a..b · comma lists · "quoted values" ·
a leading "-" negates · a bare word searches the study description.
Parity with the browser implementation is tested on a shared corpus (tests/query_corpus.json).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from sqlalchemy import and_, exists, false, func, not_, or_, select, true
from sqlalchemy.sql.elements import ColumnElement

from ..db.models import Device, Event, Patient, Study

KEYS = {
    "modality": "enum", "date": "date", "device": "text", "desc": "text", "proto": "text",
    "age": "number", "weight": "number", "acc": "text", "pid": "text", "dlp": "number",
    "ctdi": "number", "spiral": "number", "axial": "number", "loc": "number",
    "origin": "enum", "phantom": "flag", "flag": "enum", "ind": "text",
}
ALIASES = {
    "aparat": "device", "vârstă": "age", "varsta": "age", "greutate": "weight", "data": "date",
    "descriere": "desc", "protocol": "proto", "fantoma": "phantom", "fantomă": "phantom",
    "modalitate": "modality", "origine": "origin", "indicatie": "ind", "indicație": "ind",
}
YEAR_DAYS = 365.25


@dataclass
class Token:
    key: str
    value: str
    neg: bool = False
    error: str | None = None


def _lex(text: str) -> list[str]:
    out, cur, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            cur += ch
            continue
        if ch.isspace() and not quoted:
            if cur:
                out.append(cur)
            cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def split_list(v: str) -> list[str]:
    parts, cur, quoted = [], "", False
    for ch in v:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == "," and not quoted:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


_NUM = re.compile(r"^(>=|<=|>|<)?(-?\d+(?:\.\d+)?)$")
_RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)?\.\.(-?\d+(?:\.\d+)?)?$")


def number_cond(v: str) -> tuple[str, float, float] | None:
    """-> (op, a, b) with op in = > >= < <= range."""
    m = _NUM.match(v)
    if m:
        return (m.group(1) or "=", float(m.group(2)), 0.0)
    m = _RANGE.match(v)
    if m and (m.group(1) or m.group(2)):
        lo = float(m.group(1)) if m.group(1) else float("-inf")
        hi = float(m.group(2)) if m.group(2) else float("inf")
        return ("range", lo, hi)
    return None


def date_cond(v: str, now: dt.datetime) -> tuple[dt.date, dt.date] | None:
    m = re.match(r"^(\d+)d$", v)
    if m:
        try:
            return (now - dt.timedelta(days=int(m.group(1)))).date(), now.date()
        except OverflowError:
            return dt.date(1, 1, 1), now.date()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})?\.\.(\d{4}-\d{2}-\d{2})?$", v)
    try:
        if m and (m.group(1) or m.group(2)):
            lo = dt.date.fromisoformat(m.group(1)) if m.group(1) else dt.date(1, 1, 1)
            hi = dt.date.fromisoformat(m.group(2)) if m.group(2) else dt.date(9999, 12, 31)
            return lo, hi
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            d = dt.date.fromisoformat(v)
            return d, d
    except ValueError:
        return None
    return None


def parse(text: str | None) -> list[Token]:
    tokens = []
    for raw in _lex(text or ""):
        neg, body = False, raw
        if body.startswith("-") and len(body) > 1:
            neg, body = True, body[1:]
        i = body.find(":")
        if i < 0:
            k = ALIASES.get(body.lower(), body.lower())
            if KEYS.get(k) == "flag":
                tokens.append(Token(k, "", neg))
            else:
                tokens.append(Token("text", body.replace('"', ""), neg))
            continue
        raw_key = body[:i].lower()
        key = ALIASES.get(raw_key, raw_key)
        tok = Token(key, body[i + 1:], neg)
        if key not in KEYS:
            tok.error = f"unknown key {raw_key!r}"
        elif KEYS[key] == "number" and not number_cond(tok.value):
            tok.error = "expected a number, >n, <=n or a..b"
        elif KEYS[key] == "date" and not date_cond(tok.value, dt.datetime.now()):
            tok.error = "expected YYYY-MM-DD..YYYY-MM-DD or 30d"
        tokens.append(tok)
    return tokens


def _num(col, v: str) -> ColumnElement:
    op, a, b = number_cond(v)
    return {
        "=": lambda: col == a, ">": lambda: col > a, ">=": lambda: col >= a,
        "<": lambda: col < a, "<=": lambda: col <= a,
        "range": lambda: and_(col >= a, col <= b),
    }[op]()


def _age(v: str) -> ColumnElement:
    """Age in completed years compared on age_days (years = floor(days / 365.25))."""
    op, a, b = number_cond(v)
    d = Study.age_days
    lo = lambda n: d >= n * YEAR_DAYS  # noqa: E731 – age >= n years
    hi = lambda n: d < (n + 1) * YEAR_DAYS  # noqa: E731 – age <= n years
    cond = {
        "=": lambda: and_(lo(a), hi(a)), ">": lambda: lo(a + 1), ">=": lambda: lo(a),
        "<": lambda: d < a * YEAR_DAYS, "<=": lambda: hi(a),
        "range": lambda: and_(lo(a) if a != float("-inf") else true(),
                              hi(b) if b != float("inf") else true()),
    }[op]()
    return and_(d.is_not(None), cond)


def _contains(col, v: str) -> ColumnElement:
    return func.lower(col).contains(v.lower(), autoescape=True)


def _devices_where(*conds) -> ColumnElement:
    return Study.device_id.in_(select(Device.id).where(*conds))


def _test_patients():
    return select(Patient.id).where(Patient.is_test.is_(True))


def _indication(v: str) -> ColumnElement:
    """Studies coded with one of the indication codes (``TRM-*``: a whole chapter);
    suggestions that nobody confirmed don't count."""
    from ..db.models import Code, StudyIndication
    from ..indications import USED

    want = [x.upper() for x in split_list(v)]
    match = or_(*[Code.code.startswith(x[:-1], autoescape=True) if x.endswith("*")
                  else Code.code == x for x in want]) if want else false()
    return Study.id.in_(select(StudyIndication.study_id)
                        .join(Code, Code.id == StudyIndication.code_id)
                        .where(StudyIndication.status.in_(USED), match))


def conditions(tokens: list[Token], now: dt.datetime,
               overrides: dict[int, str] | None = None) -> list[ColumnElement]:
    """SQL conditions on Study only (device/patient criteria become IN-subqueries), so counts
    and pages need no joins and can use the study indexes."""
    conds: list[ColumnElement] = []
    phantom_allowed = False
    for t in tokens:
        if t.error:
            continue
        v = t.value
        k = t.key
        if k in ("text", "desc"):
            c = _contains(func.coalesce(Study.study_description, ""), v)
        elif k == "modality":
            mods = [m.upper() for m in split_list(v)]
            c = Study.modality.in_(mods)
            if overrides:  # a device-level override replaces the stored modality
                c = or_(and_(c, or_(Study.device_id.is_(None),
                                    Study.device_id.not_in(list(overrides)))),
                        Study.device_id.in_([d for d, m in overrides.items() if m in mods]
                                            or [-1]))
        elif k == "date":
            lo, hi = date_cond(v, now)
            end = (dt.datetime.combine(hi, dt.time()) + dt.timedelta(days=1)
                   if hi < dt.date.max else dt.datetime.max)
            c = and_(Study.study_datetime >= dt.datetime.combine(lo, dt.time()),
                     Study.study_datetime < end)
        elif k == "device":
            hay = [func.coalesce(Device.display_name, ""), func.coalesce(Device.model_name, ""),
                   func.coalesce(Device.station_name, "")]
            c = _devices_where(or_(*[_contains(col, x) for x in split_list(v) for col in hay]))
        elif k == "proto":
            c = exists(select(Event.id).where(
                Event.study_id == Study.id, _contains(func.coalesce(Event.protocol, ""), v)))
        elif k == "acc":
            acc = func.coalesce(Study.accession, "")
            c = acc.startswith(v[:-1], autoescape=True) if v.endswith("*") else acc == v
        elif k == "pid":
            c = Study.patient_id.in_(select(Patient.id).where(Patient.pseudo_id == v.upper()))
        elif k == "age":
            c = _age(v)
        elif k == "weight":
            c = and_(Study.weight_kg.is_not(None), Study.weight_kg > 0, _num(Study.weight_kg, v))
        elif k == "dlp":
            c = _num(func.coalesce(Study.total_dlp, 0), v)
        elif k == "ctdi":
            c = _num(func.coalesce(Study.ctdivol_max, 0), v)
        elif k == "spiral":
            c = _num(Study.n_spiral, v)
        elif k == "axial":
            c = _num(Study.n_axial, v)
        elif k == "loc":
            c = _num(Study.n_localizer, v)
        elif k == "origin":
            if v == "any":
                c = true()
            else:
                ext = _devices_where(Device.is_local.is_(False))
                c = ext if v == "external" else or_(Study.device_id.is_(None), not_(ext))
        elif k == "phantom":
            if not t.neg:
                phantom_allowed = True
            is_test = and_(Study.patient_id.is_not(None), Study.patient_id.in_(_test_patients()))
            conds.append(not_(is_test) if t.neg else is_test)
            continue
        elif k == "ind":
            c = _indication(v)
        elif k == "flag":
            c = false()  # no DRL mapping / alert engine yet: nothing is flagged
        else:
            continue
        conds.append(not_(c) if t.neg else c)
    if not phantom_allowed:
        conds.append(or_(Study.patient_id.is_(None),
                         Study.patient_id.not_in(_test_patients())))
    return conds
