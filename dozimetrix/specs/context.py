"""The run of one spec: who asked, with which parameters, over what."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .loader import Spec


@dataclass
class Context:
    session: Session
    spec: Spec
    params: dict
    conds: list  # SQL conditions on Study, from the typed query
    start: dt.datetime | None
    end: dt.datetime | None
    now: dt.datetime
    secret: bytes = b""
    username: str = ""
    names: dict = field(default_factory=dict)  # device id -> display name
    state: dict = field(default_factory=dict)  # scratch for kinds / extract caches

    def rule(self, name: str):
        return self.spec.rules[name]
