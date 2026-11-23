"""Find, read and check spec files.

Specs come from two places: the ones bundled in ``seeds/specs/`` and the folder the site drops its
own into (``[specs] dir``, default ``<config dir>/specs/``). A site spec with the same id replaces
the bundled one. The folder is re-read when a file's modification time changes, so a dropped file
shows up without a restart. A spec with an error is not fatal: it is listed in ``Catalog.problems``
with the reason, and the others keep working.

A spec file::

    [spec]      id, kind, version, title{en,ro}, authority, country, legal, formats, access
    [params]    what the person chooses (period, query, …)
    [rules]     the constants, each ``{ value = 30, article = "art. 7(3)" }``
    [deadline]  optional: when it is due, shown on the overview
    ...         the body of its kind (see ``specs/kinds``)

``"${rules.name}"`` anywhere in the body is replaced by that rule's value.
"""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import registry

BUNDLED = Path(__file__).resolve().parent.parent / "seeds" / "specs"
PERIODS = ("year", "quarter", "month", "range", "none")
FORMATS = ("xlsx", "csv", "json", "zip")
_REF = re.compile(r"\$\{rules\.([a-z0-9_]+)\}")


@dataclass
class Spec:
    id: str
    kind: str
    version: str
    title: dict[str, str]
    authority: str
    country: str
    legal: str
    formats: list[str]
    access: list[str]
    params: dict
    rules: dict[str, object]
    rules_meta: dict[str, dict]  # name -> {"article": ..., "note": ...}
    deadline: dict | None
    body: dict  # the whole file, ${rules.x} resolved
    path: Path
    bundled: bool
    mtime: float = 0.0

    def rule(self, name: str):
        return self.rules[name]

    def label(self, lang: str = "en") -> str:
        return self.title.get(lang) or self.title.get("en") or self.id

    def public(self, lang: str = "en") -> dict:
        return {"id": self.id, "kind": self.kind, "version": self.version, "title": self.title,
                "label": self.label(lang), "authority": self.authority, "country": self.country,
                "legal": self.legal, "formats": self.formats, "params": self.params,
                "rules": [{"name": n, "value": v, **self.rules_meta.get(n, {})}
                          for n, v in self.rules.items()],
                "deadline": self.deadline, "bundled": self.bundled}


@dataclass
class Catalog:
    specs: dict[str, Spec] = field(default_factory=dict)
    problems: list[dict] = field(default_factory=list)


class SpecError(ValueError):
    pass


def _resolve(node, rules: dict):
    """Replace ``${rules.x}``; a string that is only a reference keeps the rule's type."""
    if isinstance(node, str):
        whole = _REF.fullmatch(node)
        if whole:
            if whole.group(1) not in rules:
                raise SpecError(f"unknown rule {whole.group(1)!r}")
            return rules[whole.group(1)]

        def sub(m):
            if m.group(1) not in rules:
                raise SpecError(f"unknown rule {m.group(1)!r}")
            return str(rules[m.group(1)])
        return _REF.sub(sub, node)
    if isinstance(node, list):
        return [_resolve(x, rules) for x in node]
    if isinstance(node, dict):
        return {k: _resolve(v, rules) for k, v in node.items()}
    return node


def parse(path: Path, bundled: bool = False) -> Spec:
    """Read and check one spec file; raises SpecError with what is wrong."""
    from . import kinds  # noqa: F401  (registers the built-in kinds and fields)

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise SpecError(f"cannot read: {e}") from e
    meta = raw.get("spec")
    if not isinstance(meta, dict):
        raise SpecError("missing [spec] table")
    for k in ("id", "kind", "version", "title"):
        if not meta.get(k):
            raise SpecError(f"[spec] needs {k}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", meta["id"]):
        raise SpecError("[spec] id is lower-case letters, digits and . _ -")
    title = meta["title"] if isinstance(meta["title"], dict) else {"en": str(meta["title"])}
    formats = list(meta.get("formats", ["xlsx"]))
    if not formats or any(f not in FORMATS for f in formats):
        raise SpecError(f"formats are {', '.join(FORMATS)}")
    rules, rules_meta = {}, {}
    for name, v in (raw.get("rules") or {}).items():
        if isinstance(v, dict) and "value" in v:
            rules[name] = v["value"]
            rules_meta[name] = {k: x for k, x in v.items() if k != "value"}
        else:
            rules[name] = v
    body = _resolve({k: v for k, v in raw.items() if k not in ("spec", "rules")}, rules)
    params = body.pop("params", {}) or {}
    period = (params.get("period") or {}).get("type", "none")
    if period not in PERIODS:
        raise SpecError(f"params.period.type is one of {', '.join(PERIODS)}")
    deadline = body.pop("deadline", None)
    if deadline is not None:
        if not isinstance(deadline, dict) or not deadline.get("id") or "day" not in deadline:
            raise SpecError("[deadline] needs id and day")
        if period not in ("quarter", "year", "month"):
            raise SpecError("[deadline] needs params.period.type quarter, year or month")
        if "month" not in deadline and "months_after_period" not in deadline:
            raise SpecError("[deadline] needs months_after_period or month")
    spec = Spec(id=meta["id"], kind=meta["kind"], version=str(meta["version"]), title=title,
                authority=meta.get("authority", ""), country=meta.get("country", ""),
                legal=meta.get("legal", ""), formats=formats,
                access=list(meta.get("access", ["exporter"])), params=params, rules=rules,
                rules_meta=rules_meta, deadline=deadline, body=body, path=path,
                bundled=bundled, mtime=path.stat().st_mtime)
    k = registry.KINDS.get(spec.kind)
    if k is None:
        raise SpecError(f"unknown kind {spec.kind!r}; one of {', '.join(sorted(registry.KINDS))}")
    errors = k.validate(spec)
    if errors:
        raise SpecError("; ".join(errors))
    return spec


_cache: dict[Path, tuple[float, Spec | str]] = {}


def discover(specs_dir: Path | None) -> Catalog:
    """Every valid spec, bundled first then the site's (a site spec replaces a bundled id)."""
    from . import kinds  # noqa: F401  (registers the built-in kinds)

    cat = Catalog()
    folders = [(BUNDLED, True)] + ([(Path(specs_dir), False)] if specs_dir else [])
    for folder, bundled in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.toml")):
            mtime = path.stat().st_mtime
            hit = _cache.get(path)
            if hit is None or hit[0] != mtime:
                try:
                    hit = (mtime, parse(path, bundled))
                except SpecError as e:
                    hit = (mtime, str(e))
                _cache[path] = hit
            if isinstance(hit[1], str):
                cat.problems.append({"file": str(path), "error": hit[1]})
            else:
                cat.specs[hit[1].id] = hit[1]
    return cat


# ------------------------------------------------------------------ periods and parameters


def _quarter(year: int, q: int) -> tuple[dt.datetime, dt.datetime]:
    if q not in (1, 2, 3, 4):
        raise SpecError("quarter is 1..4")
    start = dt.datetime(year, 3 * q - 2, 1)
    end = dt.datetime(year + 1, 1, 1) if q == 4 else dt.datetime(year, 3 * q + 1, 1)
    return start, end


def window(spec: Spec, params: dict) -> tuple[dt.datetime | None, dt.datetime | None]:
    """The period the person chose as [start, end), narrowed by ``[window] months`` when the spec
    says only some months of it count (e.g. February and March of a year)."""
    kind = (spec.params.get("period") or {}).get("type", "none")
    try:
        if kind == "year":
            y = int(params["year"])
            start, end = dt.datetime(y, 1, 1), dt.datetime(y + 1, 1, 1)
        elif kind == "quarter":
            start, end = _quarter(int(params["year"]), int(params["quarter"]))
        elif kind == "month":
            y, m = (int(x) for x in str(params["month"]).split("-"))
            start = dt.datetime(y, m, 1)
            end = dt.datetime(y + (m == 12), m % 12 + 1, 1)
        elif kind == "range":
            start = dt.datetime.fromisoformat(str(params["from"]))
            end = dt.datetime.fromisoformat(str(params["to"])) + dt.timedelta(days=1)
        else:
            return None, None
    except (KeyError, ValueError) as e:
        raise SpecError(f"the period needs a valid {kind}: {e}") from e
    months = (spec.body.get("window") or {}).get("months")
    if months and kind == "year":
        start = dt.datetime(start.year, min(months), 1)
        last = max(months)
        end = dt.datetime(start.year + (last == 12), last % 12 + 1, 1)
    return start, end


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


def check_params(spec: Spec, raw: dict) -> dict:
    """The params a run was given, with defaults and types; raises SpecError."""
    out = dict(raw)
    for name, p in spec.params.items():
        if name == "period" or not isinstance(p, dict):
            continue
        if out.get(name) in (None, ""):
            if "default" in p:
                out[name] = p["default"]
            elif p.get("required"):
                raise SpecError(f"parameter {name} is required")
            continue
        t = p.get("type", "str")
        try:
            out[name] = {"int": int, "float": float, "str": str, "query": str,
                         "bool": _truthy}[t](out[name])
        except (KeyError, ValueError) as e:
            raise SpecError(f"parameter {name} is not a valid {t}") from e
        if p.get("choices") and out[name] not in p["choices"]:
            raise SpecError(f"parameter {name} is one of {', '.join(map(str, p['choices']))}")
    return out
