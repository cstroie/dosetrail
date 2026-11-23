"""The extension surface: five registries, filled with decorators.

A spec (TOML) can only name things registered here; it can never run code. A new field, transform,
filter, kind or writer is a small function with a decorator, in the program or in a trusted plugin
(``plugins.py``). Every entry's docstring is shown by ``dosimetrix export fields``.

    from dosimetrix.specs.registry import field, transform

    @field("patient.initials", identifying=True)
    def initials(row):
        "The patient's initials."
        ...

    @transform("upper")
    def upper(value):
        return value.upper() if value else value
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    fn: Callable  # fn(row) -> value; row is specs.extract.Row
    unit: str = ""
    identifying: bool = False  # personal data: a spec using it needs the pid role, audited
    doc: str = ""


FIELDS: dict[str, Field] = {}
TRANSFORMS: dict[str, Callable] = {}
FILTERS: dict[str, Callable] = {}
KINDS: dict[str, object] = {}  # name -> module/object with validate(spec) and run(ctx)
WRITERS: dict[str, Callable] = {}


def field(name: str, *, unit: str = "", identifying: bool = False):
    def deco(fn):
        FIELDS[name] = Field(name, fn, unit, identifying, (fn.__doc__ or "").strip())
        return fn
    return deco


def transform(name: str):
    """``fn(value, *args)``: used in a spec as ``transform = "round:2"`` (args after colons)."""
    def deco(fn):
        TRANSFORMS[name] = fn
        return fn
    return deco


def filter_(name: str):
    """``fn((ctx, units), *args) -> units``: drops examinations from the dict a kind gathered
    (spec: ``exclude = ["name:arg"]``)."""
    def deco(fn):
        FILTERS[name] = fn
        return fn
    return deco


def kind(name: str):
    """A kind: a class or module with ``validate(spec) -> list[str]`` and ``run(ctx) -> Result``."""
    def deco(obj):
        KINDS[name] = obj
        return obj
    return deco


def writer(fmt: str):
    """``fn(result, target, ctx)``: writes a Result in the format ``fmt``."""
    def deco(fn):
        WRITERS[fmt] = fn
        return fn
    return deco


def call(spec_expr: str, value, table: dict[str, Callable]):
    """Apply "name" or "name:arg1:arg2" from ``table`` (transforms and filters)."""
    name, *args = spec_expr.split(":")
    fn = table.get(name)
    if fn is None:
        raise KeyError(f"unknown {name!r}; one of {', '.join(sorted(table))}")
    return fn(value, *args)
