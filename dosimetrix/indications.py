"""Clinical indications: the request's free text -> a ro-nom-indications code.

Sources, best first:
1. the HIS request (``study_request``): the referral-guideline item the doctor picked
   (``guideline`` is its chapter, e.g. "Traumatisme / Cap"; the indication text is then the
   item's name), else the free text;
2. ``study.indication_text`` (DICOM Reason for the Requested Procedure / Reason for Study /
   Admitting Diagnoses Description, or a CSV import).

Matching, per text:
* a **human decision** (``code_mapping``: modality "ind", field "indication", the
  normalised text) always wins → status ``confirmed``;
* otherwise the text is compared with every code meaning: character-trigram similarity of
  the texts without spaces (so "cranio-encefalic" meets "cranioencefalic"), and how many
  of the meaning's words (first 5 letters) the text contains. A guideline chapter that is
  not "neincadrabila" adds its words. For a child (< 18 y) a PED-… code wins over an
  equally good adult code, and the other way round for adults;
* score ≥ 0.75 → ``auto``; 0.45–0.75 → ``suggested`` (shown for review, not used in
  statistics); lower → no code.

A study whose indication was set by hand (status ``manual``) is never recomputed.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .classify import normalise
from .db.models import (
    Code,
    CodeMapping,
    CodeSystem,
    Study,
    StudyIndication,
    StudyRequest,
)

SYSTEM = "ro-nom-indications"
FAMILY, FIELD = "ind", "indication"
AUTO, SUGGEST = 0.75, 0.5
NOT_CLASSIFIABLE = re.compile(r"neincadrabil|neclasific|nespecific")
CHILD_DAYS = 18 * 365.25
STOP = {"de", "si", "cu", "la", "in", "din", "pe", "al", "a", "o", "un", "fara", "sau",
        "pentru", "penntru", "vezi", "capitolul", "rog", "the", "of", "and"}
USED = ("auto", "confirmed", "manual")  # statuses statistics use


def _stems(text: str) -> set[str]:
    return {w[:5] for w in text.split() if len(w) > 2 and w not in STOP}


def _trigrams(text: str) -> set[str]:
    t = text.replace(" ", "")
    return {t[i:i + 3] for i in range(len(t) - 2)} if len(t) > 2 else {t}


@dataclass(frozen=True)
class Entry:
    code_id: int
    code: str
    meaning: str
    stems: frozenset
    grams: frozenset
    paediatric: bool


@dataclass
class Match:
    code_id: int
    code: str
    score: float
    status: str
    source: str
    text: str


BEST_MEMO_MAX = 20000  # distinct (text, child) pairs kept per Matcher


class Matcher:
    def __init__(self, session: Session):
        system = session.scalar(select(CodeSystem).filter_by(key=SYSTEM))
        if system is None:
            raise ValueError("the indication vocabulary is not loaded (dosimetrix seed)")
        self.entries: list[Entry] = []
        for c in session.scalars(select(Code).filter_by(system_id=system.id)):
            n = normalise(c.meaning)
            self.entries.append(Entry(c.id, c.code, c.meaning, frozenset(_stems(n)),
                                      frozenset(_trigrams(n)), c.code.startswith("PED-")))
        self.by_code = {e.code: e for e in self.entries}
        self.human = {m.match_value: m.code_id for m in session.scalars(
            select(CodeMapping).filter_by(modality=FAMILY, match_field=FIELD))}
        self.code_of = {e.code_id: e.code for e in self.entries}
        self._best: dict[tuple[str, bool | None], tuple[Entry | None, float]] = {}

    def score(self, text: str, entry: Entry) -> float:
        """0–1. Needs shared words: trigram likeness alone ("infectie respiratorie" vs
        "infectie urinara") is not enough."""
        return self._score(_trigrams(text), _stems(text), entry)

    @staticmethod
    def _score(grams: set, stems: set, entry: Entry) -> float:
        """``score`` with the text's trigrams and stems already computed (once per text)."""
        # words of the meaning (first 5 letters) found in the text: "cranio-encefalic" and
        # "cranioencefalic" share "crani"
        cover = len(stems & entry.stems) / len(entry.stems) if entry.stems else 0
        if cover < 0.3:
            return 0.0
        dice = 2 * len(grams & entry.grams) / (len(grams) + len(entry.grams) or 1)
        contain = len(grams & entry.grams) / (len(entry.grams) or 1)
        return round(max(dice, 0.5 * cover + 0.5 * contain) * min(1.0, 0.4 + cover), 3)

    def best(self, text: str, child: bool | None) -> tuple[Entry | None, float]:
        """The best entry for a normalised text; remembered per (text, child), as the
        same indications come back study after study."""
        if not text:
            return None, 0.0
        key = (text, child)
        hit = self._best.get(key)
        if hit is None:
            if len(self._best) >= BEST_MEMO_MAX:
                self._best.clear()
            hit = self._best[key] = self._best_uncached(text, child)
        return hit

    def _best_uncached(self, text: str, child: bool | None) -> tuple[Entry | None, float]:
        grams, stems = _trigrams(text), _stems(text)
        scored = sorted(((self._score(grams, stems, e), e) for e in self.entries),
                        key=lambda x: -x[0])[:8]
        if not scored or scored[0][0] == 0:
            return None, 0.0
        top = scored[0][0]
        # the age-specific variant of the same item (PED-TCE vs NEU-TCE) is usually a few
        # words longer or shorter: prefer it when it is nearly as good
        lead = scored[0][1]
        if child is not None and lead.paediatric != child:
            # the same item's other-age variant (PED-TUS "Tuse cronică" vs RSP-TUS "Tuse
            # cronică neexplicată"): shares most words with the leader, or, when the
            # leader itself says "for children see the Paediatrics chapter", its first words
            redirect = bool(child) and "pediatri" in normalise(lead.meaning)
            pool = self.entries
            pref = []
            for e in pool:
                if e.paediatric != child or e is lead:
                    continue
                shared = len(e.stems & lead.stems)
                if (redirect and shared >= 2) or shared / len(e.stems | lead.stems) >= 0.5:
                    pref.append((self._score(grams, stems, e), e))
            if pref:
                s, e = max(pref, key=lambda x: x[0])
                return e, max(s, min(top, AUTO))
            if child is False and lead.paediatric:
                return lead, min(top, AUTO - 0.05)  # a paediatric item for an adult: review
        return lead, top

    def match(self, texts: list[tuple[str, str]], child: bool | None) -> Match | None:
        """texts: (source, raw text), best source first."""
        for source, raw in texts:
            norm = normalise(raw)
            if norm and norm in self.human:
                cid = self.human[norm]
                return Match(cid, self.code_of.get(cid, "?"), 1.0, "confirmed", source,
                             norm[:255])
        best = None
        for source, raw in texts:
            norm = normalise(raw)
            if not norm:
                continue
            e, s = self.best(norm, child)
            if e is not None and (best is None or s > best.score):
                best = Match(e.code_id, e.code, round(s, 3),
                             "auto" if s >= AUTO else "suggested", source, norm[:255])
        return best if best and best.score >= SUGGEST else None


def _texts(req: StudyRequest | None, study: Study) -> list[tuple[str, str]]:
    out = []
    if req is not None:
        guide = req.guideline or ""
        picked = guide and not NOT_CLASSIFIABLE.search(normalise(guide))
        if req.indication:
            out.append(("his_guide" if picked else "his_text", req.indication))
        if picked:
            out.append(("his_guide", f"{guide} {req.indication or ''}"))
    if study.indication_text:
        out.append((study.indication_source or "dicom", study.indication_text))
    return out


def update(session: Session, study_ids: Iterable[int] | None = None,
           matcher: Matcher | None = None, batch: int = 2000) -> Counter:
    """(Re)code the given studies, or every study with an indication text. Without the
    vocabulary (not seeded yet) nothing happens: imports must not fail on it."""
    stats: Counter = Counter()
    try:
        m = matcher or Matcher(session)
    except ValueError:
        stats["no_vocabulary"] += 1
        return stats
    if study_ids is None:
        ids = sorted(set(session.scalars(select(StudyRequest.study_id))) | set(session.scalars(
            select(Study.id).where(Study.indication_text.is_not(None)))))
    else:
        ids = sorted(set(study_ids))
    for i in range(0, len(ids), batch):
        chunk = ids[i:i + batch]
        manual = set(session.scalars(select(StudyIndication.study_id).where(
            StudyIndication.study_id.in_(chunk), StudyIndication.status == "manual")))
        reqs = {r.study_id: r for r in session.scalars(
            select(StudyRequest).where(StudyRequest.study_id.in_(chunk)))}
        studies = session.scalars(select(Study).where(Study.id.in_(chunk)))
        todo = [sid for sid in chunk if sid not in manual]
        session.execute(delete(StudyIndication).where(StudyIndication.study_id.in_(todo)))
        for st in studies:
            if st.id in manual:
                stats["manual_kept"] += 1
                continue
            child = None if st.age_days is None else st.age_days < CHILD_DAYS
            res = m.match(_texts(reqs.get(st.id), st), child)
            if res is None:
                stats["no_text" if not _texts(reqs.get(st.id), st) else "uncoded"] += 1
                continue
            session.add(StudyIndication(study_id=st.id, code_id=res.code_id, status=res.status,
                                        score=res.score, source=res.source,
                                        matched_text=res.text))
            stats[res.status] += 1
        session.flush()
    return stats


def set_mapping(session: Session, raw: str, code: Code | None) -> CodeMapping | None:
    """A human decision for one normalised text; ``code=None`` removes it."""
    from .classify import set_mapping as _set

    return _set(session, FAMILY, FIELD, raw, code)


def studies_with_text(session: Session, raw: str) -> list[int]:
    norm = normalise(raw)
    ids = {sid for sid, t in session.execute(
        select(StudyRequest.study_id, StudyRequest.indication)) if normalise(t) == norm}
    ids |= {sid for sid, t in session.execute(
        select(Study.id, Study.indication_text).where(Study.indication_text.is_not(None)))
        if normalise(t) == norm}
    return sorted(ids)


def set_study(session: Session, study_id: int, code: Code | None) -> None:
    """Code one study by hand (``manual``), or clear that so matching applies again."""
    row = session.get(StudyIndication, study_id)
    if code is None:
        if row is not None and row.status == "manual":
            session.delete(row)
        session.flush()
        update(session, [study_id])
        return
    row = row or StudyIndication(study_id=study_id)
    row.code_id, row.status, row.score, row.source = code.id, "manual", 1.0, "manual"
    session.add(row)
    session.flush()


def review(session: Session, limit: int = 200) -> dict:
    """Texts to review: uncoded or only suggested, most frequent first, with the best
    suggestion; plus coverage counts and the human decisions."""
    m = Matcher(session)
    counts: Counter = Counter()
    sample: dict[str, str] = {}
    child_n: Counter = Counter()
    coded = {si.study_id: si for si in session.scalars(select(StudyIndication))}
    rows = session.execute(
        select(Study.id, Study.age_days, StudyRequest.indication, StudyRequest.guideline,
               Study.indication_text)
        .outerjoin(StudyRequest, StudyRequest.study_id == Study.id)
        .where((StudyRequest.study_id.is_not(None)) | (Study.indication_text.is_not(None))))
    total = 0
    for sid, age, ind, _guide, txt in rows:
        total += 1
        si = coded.get(sid)
        if si is not None and si.status in USED:
            continue
        raw = ind or txt
        norm = normalise(raw)
        if not norm:
            continue
        counts[norm] += 1
        sample.setdefault(norm, raw)
        if age is not None and age < CHILD_DAYS:
            child_n[norm] += 1
    items = []
    for norm, n in counts.most_common(limit):
        child = child_n[norm] * 2 >= n
        scored = sorted(((m.score(norm, e), e) for e in m.entries), key=lambda x: -x[0])[:3]
        items.append({"text": sample[norm], "normalised": norm, "studies": n,
                      "suggestions": [{"code": e.code, "meaning": e.meaning,
                                       "score": round(s, 2)} for s, e in scored if s > 0.2],
                      "children": child})
    by_status = Counter(si.status for si in coded.values())
    decisions = [{"text": mp.match_value, "code": m.code_of.get(mp.code_id),
                  "meaning": m.by_code[m.code_of[mp.code_id]].meaning
                  if mp.code_id in m.code_of else None}
                 for mp in session.scalars(select(CodeMapping).filter_by(
                     modality=FAMILY, match_field=FIELD).order_by(CodeMapping.match_value))]
    return {"studies_with_text": total, "coded": dict(by_status), "items": items,
            "decisions": decisions}


def of_study(session: Session, study_id: int) -> dict | None:
    """The study's indication for the detail page (code, meaning, how it was found)."""
    row = session.execute(select(StudyIndication, Code)
                          .join(Code, Code.id == StudyIndication.code_id)
                          .where(StudyIndication.study_id == study_id)).first()
    st = session.get(Study, study_id)
    req = session.get(StudyRequest, study_id)
    text = (req.indication if req is not None else None) or (st.indication_text if st else None)
    if row is None:
        return {"code": None, "text": text} if text else None
    si, code = row
    return {"code": code.code, "meaning": code.meaning, "status": si.status,
            "source": si.source, "score": si.score, "text": text}


def summary(session: Session) -> dict:
    return dict(session.execute(select(StudyIndication.status, func.count())
                                .group_by(StudyIndication.status)).all())
