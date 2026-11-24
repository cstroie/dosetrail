"""What a kind returns and what a writer writes."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Sheet:
    name: str
    columns: list[str]
    rows: list[list]
    widths: list[float] | None = None
    flags: list[dict | None] | None = None  # per row, e.g. {"low_n": ["Room 2"]}: the UI badges it
    meta: dict = field(default_factory=dict)  # kind-specific facts (patients, codes, …)


@dataclass
class Book:
    """One workbook: a spec makes one, or several (one per device, say) that go in a zip."""

    filename: str
    sheets: list[Sheet]
    title: str = ""
    info: list[tuple[str, object]] = field(default_factory=list)  # the first sheet's rows
    meta: dict = field(default_factory=dict)  # e.g. family, device


@dataclass
class Result:
    books: list[Book]
    notes: dict = field(default_factory=dict)
    extra: dict = field(default_factory=dict)  # kind-specific, goes into the JSON as it is
    summary: list[dict] | None = None  # for the preview of a multi-workbook run
