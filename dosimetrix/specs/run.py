"""Run a spec: check the parameters and the roles, build the context, call its kind, write."""

from __future__ import annotations

import datetime as dt
from typing import IO

from sqlalchemy.orm import Session

from . import registry
from .context import Context
from .loader import Spec, SpecError, check_params, window
from .result import Result


class Forbidden(PermissionError):
    pass


def fields_used(spec: Spec) -> set[str]:
    """Registered field names the spec mentions anywhere (conservative: a string equal to a field
    name counts), so the roles it needs can be known without running it."""
    found: set[str] = set()

    def walk(node):
        if isinstance(node, str):
            if node in registry.FIELDS:
                found.add(node)
        elif isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for x in node.values():
                walk(x)
    walk(spec.body)
    return found


def needs_pid(spec: Spec) -> bool:
    k = registry.KINDS[spec.kind]
    return any(registry.FIELDS[f].identifying for f in fields_used(spec)) or bool(
        getattr(k, "identifying", False))


def check_roles(spec: Spec, roles: set[str]) -> None:
    """``access`` names the roles that may run it (admin always may); identifying fields add pid,
    which no spec can waive."""
    if not roles & (set(spec.access) | {"admin"}):
        raise Forbidden(f"one of the roles {', '.join(spec.access)} is required")
    if needs_pid(spec) and "pid" not in roles:
        raise Forbidden("the pid role is required: this export holds patient identifiers")


def build(session: Session, spec: Spec, raw_params: dict, *, secret: bytes = b"",
          now: dt.datetime | None = None, username: str = "") -> Context:
    from ..api import data

    params = check_params(spec, raw_params)
    start, end = window(spec, params)
    now = now or dt.datetime.now()
    q = params.get("q") or ""
    conds = data.conditions(data.parse(q), now, data.overrides(session))
    return Context(session, spec, params, conds, start, end, now, secret, username,
                   data.device_names(session))


def run(ctx: Context) -> Result:
    return registry.KINDS[ctx.spec.kind].run(ctx)


def write(result: Result, fmt: str, target: IO[bytes], ctx: Context) -> None:
    if fmt not in ctx.spec.formats:
        raise SpecError(f"this spec is available as {', '.join(ctx.spec.formats)}")
    registry.WRITERS[fmt](result, target, ctx)
