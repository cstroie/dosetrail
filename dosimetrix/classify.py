"""Exam classification: local text -> standard exam codes, and on to DRL codes.

* **Rules** (seeds/classify_rules.toml) map normalised text to a nomenclator code, per
  family: rx = DX/CR/MG, rf = RF/XA, ct = CT. The first matching rule wins.
* **Human decisions** (code_mapping rows with status "confirmed") always win over rules.
* **Granularity:**
  * radiography is classified per exposure (protocol -> anatomy -> study description), and
    the study gets every distinct exam of its exposures;
  * CT and fluoroscopy are classified per study (description -> procedure -> series
    protocols).
* **Crosswalk** (seeds/crosswalk.csv) adds the DRL codes (national, European). For
  radiography this depends on the projection, so DRL codes sit on the exposure.
* Results are rows in ``classification``. Re-running replaces the automatic rows; studies
  with a confirmed (manual) classification are left alone.
"""

from __future__ import annotations

import csv
import datetime as dt
import re
import tomllib
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from .db.models import (
    Classification,
    ClassificationStatus,
    Code,
    CodeMapping,
    CodeSystem,
    Event,
    Study,
)

SEEDS = Path(__file__).parent / "seeds"
NOMENCLATOR = {"rx": "ro-nom-rx", "rf": "ro-nom-rx", "ct": "ro-nom-ct",
               "xa": "ro-insp-1255-xa"}
FAMILY = {"DX": "rx", "CR": "rx", "MG": "rx", "PX": "rx", "RF": "rf", "XA": "rf", "CT": "ct"}
STUDY_FIELDS = ("study_description", "procedure_name", "requested_procedure")
DRL_SYSTEMS = ("ro-drl-ms838-2025", "ec-rp185")
HEAD_CT = {"CT-CR", "CT-CRLD", "CT-CROR", "CT-CRMS", "CT-CRMF", "CT-CRPF", "CT-SN", "CT-SNLD",
           "CT-SNCO", "CT-TRCR", "AG-CR"}
EVENT_FIELDS = ("protocol", "anatomy")
BATCH = 2000


def normalise(text: str | None) -> str:
    """' w035 torace a p 10kg ' – lower case, no accents, alphanumerics separated by spaces."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = re.sub(r"[^0-9a-z]+", " ", t).strip()
    return f" {t} " if t else ""


_VIEW_FIELD = [
    (re.compile(r"postero ?anterior|^ ?pa ?$|\bp a\b"), "PA"),
    (re.compile(r"antero ?posterior|^ ?ap ?$|\ba p\b|frontal"), "AP"),
    (re.compile(r"lateral|\blat\b|profil|\bll\b"), "LL"),
    (re.compile(r"obli"), "OBL"),
    (re.compile(r"tangen"), "TG"),
    (re.compile(r"axial"), "AX"),
]
_VIEW_TEXT = [
    (re.compile(r"\bp ?a\b|\bpa\b"), "PA"),
    (re.compile(r"\ba ?p\b|\bap\b|\bfata\b"), "AP"),
    (re.compile(r"\blat\b|\blateral\b|profil|\bll\b|\bpr\b"), "LL"),  # "pr." = profil
    (re.compile(r"oblic|\bobl\b"), "OBL"),
]
# A chest exposure that says nothing about its projection is taken as frontal: laterals
# are named ("lat", "pr.", "profil"), and bedside chest films (mobile units send no view)
# are AP by practice. Other regions are left without a projection.
FRONTAL_BY_DEFAULT = {"RG-TH", "RG-THPT"}


def view_code(view: str | None, protocol: str | None) -> str | None:
    """Projection from the DICOM view (preferred) or the protocol name."""
    v = normalise(view)
    if v:
        for rx, code in _VIEW_FIELD:
            if rx.search(v):
                return code
    p = normalise(protocol)
    for rx, code in _VIEW_TEXT:
        if rx.search(p):
            return code
    return None


@dataclass
class Rules:
    families: dict[str, list[tuple[str, re.Pattern]]]
    contrast: dict[str, re.Pattern]

    @classmethod
    def load(cls, path: Path = SEEDS / "classify_rules.toml") -> Rules:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        fams = {f: [(r["code"], re.compile(r["match"])) for r in raw.get(f, [])]
                for f in ("rx", "rf", "ct", "xa")}
        con = {k: re.compile(v) for k, v in raw.get("contrast", {}).items()}
        return cls(fams, con)

    def match(self, family: str, text: str) -> str | None:
        if not text:
            return None
        for code, rx in self.families.get(family, ()):
            if rx.search(text):
                return code
        return None

    def contrast_mode(self, texts: Iterable[str]) -> str | None:
        joined = " ".join(t for t in texts if t)
        if not joined:
            return None
        if self.contrast["multiphase"].search(joined):
            return "MF"
        c = bool(self.contrast["contrast"].search(joined))
        n = bool(self.contrast["native"].search(joined))
        return "NC" if c and n else "C" if c else "N" if n else None


_DEFAULT: Rules | None = None


def default_rules() -> Rules:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Rules.load()
    return _DEFAULT


@dataclass
class Result:
    rows: list[dict] = field(default_factory=list)
    contrast: str | None = None


class Classifier:
    """Load rules, codes, crosswalk and confirmed mappings once; classify many studies."""

    def __init__(self, session: Session, rules: Rules | None = None):
        self.session = session
        self.rules = rules or default_rules()
        self.code_id: dict[tuple[str, str], int] = {}
        for sys_key, code, cid in session.execute(
                select(CodeSystem.key, Code.code, Code.id).join(Code.system)):
            self.code_id[(sys_key, code)] = cid
        self.code_key = {v: k for k, v in self.code_id.items()}
        self.crosswalk: dict[int, list[tuple[set[str] | None, int]]] = defaultdict(list)
        with (SEEDS / "crosswalk.csv").open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                src = self.code_id.get((r["from_system"], r["from_code"]))
                dst = self.code_id.get((r["to_system"], r["to_code"]))
                if src and dst:
                    views = None if r["views"] == "*" else set(r["views"].split("|"))
                    self.crosswalk[src].append((views, dst))
        # confirmed human mappings: (family, field, normalised text) -> (code id, mapping id)
        self.overrides: dict[tuple[str, str, str], tuple[int, int]] = {}
        for m in session.scalars(select(CodeMapping).where(
                CodeMapping.status == ClassificationStatus.confirmed)):
            self.overrides[(m.modality or "", m.match_field, m.match_value)] = (m.code_id, m.id)
        self._memo: dict[tuple[str, str, str], tuple[int, int | None] | None] = {}

    # -------------------------------------------------------------- matching

    def match(self, family: str, fld: str, raw: str | None) -> tuple[int, int | None] | None:
        """(code id, mapping id or None) for one text, or None."""
        text = normalise(raw)
        if not text:
            return None
        key = (family, fld, text)
        if key in self._memo:
            return self._memo[key]
        hit = self.overrides.get(key)
        if hit is None:
            code = self.rules.match(family, text)
            cid = self.code_id.get((NOMENCLATOR[family], code)) if code else None
            hit = (cid, None) if cid else None
        self._memo[key] = hit
        return hit

    def first(self, family: str, pairs: Iterable[tuple[str, str | None]]):
        for fld, raw in pairs:
            hit = self.match(family, fld, raw)
            if hit:
                return hit
        return None

    def drl_codes(self, code_id: int, view: str | None) -> list[int]:
        return [dst for views, dst in self.crosswalk.get(code_id, ())
                if views is None or (view is not None and view in views)]

    # -------------------------------------------------------------- one study

    def classify(self, study: dict, events: list[dict]) -> Result:
        family = FAMILY.get((study.get("modality") or "").upper())
        out = Result()
        if family is None:
            return out
        sid = study["id"]
        study_pairs = [(f, study.get(f)) for f in STUDY_FIELDS]

        def row(code_id, mapping_id, event_id=None):
            status = (ClassificationStatus.confirmed if mapping_id
                      else ClassificationStatus.auto)
            out.rows.append({"study_id": sid, "event_id": event_id, "code_id": code_id,
                             "status": status, "mapping_id": mapping_id})

        if family == "rx":
            found: dict[int, int | None] = {}
            for e in events:
                hit = self.first(family, [("protocol", e.get("protocol")),
                                          ("anatomy", e.get("anatomy")), *study_pairs])
                if not hit:
                    continue
                cid, mid = hit
                row(cid, mid, e["id"])
                view = view_code(e.get("view"), e.get("protocol"))
                if view is None and self.code_key.get(cid, ("", ""))[1] in FRONTAL_BY_DEFAULT:
                    view = "AP"
                for dst in self.drl_codes(cid, view):
                    row(dst, None, e["id"])
                found.setdefault(cid, mid)
            if not found and not events:
                hit = self.first(family, study_pairs)
                if hit:
                    found[hit[0]] = hit[1]
            for cid, mid in found.items():
                row(cid, mid)
            return out

        # CT and fluoroscopy: one exam per study
        hit = self.first(family, study_pairs)
        if hit is None:
            votes = Counter()
            for e in events:
                if e.get("is_localizer"):
                    continue
                h = self.match(family, "protocol", e.get("protocol"))
                if h:
                    votes[h] += 1
            hit = votes.most_common(1)[0][0] if votes else None
        if family == "ct":
            texts = [normalise(study.get(f)) for f in STUDY_FIELDS]
            texts += [normalise(e.get("protocol")) for e in events if not e.get("is_localizer")]
            out.contrast = self.rules.contrast_mode(texts)
        if hit:
            cid, mid = hit
            row(cid, mid)
            for dst in self.drl_codes(cid, None):
                row(self.contrast_variant(dst, out.contrast), None)
        if family == "ct":
            self._ct_series(events, hit[0] if hit else None, row)
        if family == "rf":  # interventional procedures: national code straight from text
            xa = self.first("xa", study_pairs)
            if xa is None:
                votes = Counter(h for e in events
                                if (h := self.match("xa", "protocol", e.get("protocol"))))
                xa = votes.most_common(1)[0][0] if votes else None
            if xa:
                row(xa[0], xa[1])
        return out

    def _ct_series(self, events: list[dict], study_code: int | None, row) -> None:
        """A series whose own protocol names another region than the study, head vs not
        head (a trunk series in a 'head' trauma study, a head series in a 'TAP' study),
        gets its own nomenclator and DRL codes: the head DRLs (16 cm phantom) and the body
        DRLs must not be mixed. Series that name no region, or the same kind, follow the
        study."""
        study_head = self._is_head(study_code)
        for e in events:
            if e.get("is_localizer"):
                continue
            own = self.match("ct", "protocol", e.get("protocol"))
            if not own or own[0] == study_code:
                continue
            if study_code is not None and self._is_head(own[0]) == study_head:
                continue
            cid, mid = own
            row(cid, mid, e["id"])
            for dst in self.drl_codes(cid, None):
                if self.code_key[dst][0] in DRL_SYSTEMS:
                    row(dst, None, e["id"])

    def _is_head(self, code_id: int | None) -> bool:
        return code_id is not None and self.code_key.get(code_id, ("", ""))[1] in HEAD_CT

    def contrast_variant(self, code_id: int, contrast: str | None) -> int:
        """National CT codes have a contrast variant (C -> CSC): use it for contrast exams."""
        if contrast not in ("C", "NC", "MF"):
            return code_id
        key = self.code_key.get(code_id)
        if key is None or key[0] != "ro-insp-1255-ct":
            return code_id
        return self.code_id.get((key[0], key[1] + "SC"), code_id)


# ------------------------------------------------------------------ batch run

_STUDY_COLS = (Study.id, Study.modality, Study.study_description, Study.procedure_name,
               Study.requested_procedure)
_EVENT_COLS = (Event.id, Event.study_id, Event.protocol, Event.anatomy, Event.view,
               Event.is_localizer)


def run(session: Session, study_ids: Iterable[int] | None = None,
        progress: Callable[[str], None] | None = None,
        classifier: Classifier | None = None) -> Counter:
    """(Re)classify the given studies, or all. Returns counters."""
    clf = classifier or Classifier(session)
    stats: Counter = Counter()
    conn = session.connection()
    if study_ids is None:
        lo = session.scalar(select(func.min(Study.id))) or 0
        hi = session.scalar(select(func.max(Study.id))) or -1
        chunks = ((a, min(a + BATCH - 1, hi)) for a in range(lo, hi + 1, BATCH))
        where = lambda col, ab: col.between(*ab)  # noqa: E731
    else:
        ids = sorted(set(study_ids))
        chunks = (ids[i:i + BATCH] for i in range(0, len(ids), BATCH))
        where = lambda col, chunk: col.in_(chunk)  # noqa: E731
    started = dt.datetime.now()
    for n, chunk in enumerate(chunks):
        studies = [dict(r._mapping) for r in conn.execute(
            select(*_STUDY_COLS).where(where(Study.id, chunk)))]
        if not studies:
            continue
        ids = [s["id"] for s in studies]
        manual = set(conn.scalars(select(Classification.study_id).where(
            Classification.study_id.in_(ids), Classification.mapping_id.is_(None),
            Classification.status == ClassificationStatus.confirmed)))
        events: dict[int, list[dict]] = defaultdict(list)
        for r in conn.execute(select(*_EVENT_COLS).where(Event.study_id.in_(ids))
                              .order_by(Event.id)):
            events[r.study_id].append(dict(r._mapping))
        rows, contrast = [], []
        for s in studies:
            if s["id"] in manual:
                stats["manual_kept"] += 1
                continue
            res = clf.classify(s, events.get(s["id"], []))
            rows.extend(res.rows)
            stats["studies"] += 1
            stats["classified" if any(r["event_id"] is None for r in res.rows)
                  else "unclassified"] += 1
            if res.contrast:
                contrast.append({"sid": s["id"], "cm": res.contrast})
        todo = [i for i in ids if i not in manual]
        conn.execute(delete(Classification).where(Classification.study_id.in_(todo)))
        if rows:
            conn.execute(insert(Classification), rows)
            stats["rows"] += len(rows)
        if contrast:
            from sqlalchemy import bindparam

            conn.execute(update(Study.__table__)
                         .where(Study.__table__.c.id == bindparam("sid"))
                         .values(contrast_mode=bindparam("cm")), contrast)
        if progress and n % 50 == 49:
            progress(f"{stats['studies']} studies, {stats['rows']} rows, "
                     f"{(dt.datetime.now() - started).total_seconds():.0f} s")
    return stats


def set_mapping(session: Session, family: str, fld: str, raw: str, code: Code | None,
                ) -> CodeMapping | None:
    """Record a human decision for one text; ``code=None`` removes it (rules apply again)."""
    text = normalise(raw)
    existing = session.scalar(select(CodeMapping).filter_by(
        modality=family, match_field=fld, match_value=text))
    if code is None:
        if existing is not None:
            session.delete(existing)
        session.flush()
        return None
    m = existing or CodeMapping(modality=family, match_field=fld, match_value=text,
                                code_id=code.id)
    m.code_id, m.status = code.id, ClassificationStatus.confirmed
    session.add(m)
    session.flush()
    return m


def studies_with_text(session: Session, family: str, fld: str, raw: str) -> list[int]:
    """Study ids whose field normalises to the same text as ``raw`` (to reclassify)."""
    text = normalise(raw)
    mods = [m for m, f in FAMILY.items() if f == family]
    if fld in EVENT_FIELDS:
        col = getattr(Event, fld)
        values = [v for v in session.scalars(
            select(col).join(Study, Study.id == Event.study_id)
            .where(Study.modality.in_(mods)).distinct()) if normalise(v) == text]
        if not values:
            return []
        return list(session.scalars(select(Event.study_id).where(col.in_(values)).distinct()))
    col = getattr(Study, fld)
    values = [v for v in session.scalars(select(col).where(Study.modality.in_(mods)).distinct())
              if normalise(v) == text]
    if not values:
        return []
    return list(session.scalars(select(Study.id).where(col.in_(values))))
