"""Idempotent storage of parsed DICOM objects.

Rules
* An object (SOP Instance UID) is processed once; re-sending it is a no-op ("duplicate").
* Studies are merged by Study Instance UID; events are unique per (study, event UID), so a
  cumulative RDSR (Siemens sends one after every series) only adds the new events.
* Projection dose reports are authoritative over image headers for the same study: header
  events are removed when an RDSR arrives, and later headers are not added ("superseded").
* An exact duplicate exposure (same second, DAP or glandular dose, kVp, mAs, side) is not
  added twice – e.g. the 'for processing' and 'for presentation' copies of one radiograph
  or mammogram.
* Study totals and derived columns are recomputed for the touched study.
* One radiopharmaceutical administration seen in two studies of the same patient (an
  early and a delayed PET, sent as two studies) counts once: on the earliest study. The
  other study's copy is marked ``same_administration_as`` and adds no activity
  (settle_administrations).
"""

from __future__ import annotations

import datetime as dt
import time
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import demographics
from ..db.models import (
    AgePrecision,
    Device,
    Event,
    HisLookup,
    HisServer,
    IngestedObject,
    Institution,
    Patient,
    SizeSource,
    Study,
)
from ..security import pseudonym
from .identity import device_key, patient_key
from .parse import ParsedObject, age_from

STUDY_FIELDS = ("accession", "study_datetime", "study_description", "requested_procedure",
                "procedure_name", "performing_physician", "operator_name",
                "referring_physician", "indication_text", "indication_source")


@dataclass
class Result:
    status: str  # imported | duplicate | ignored | superseded | failed
    study_id: int | None = None
    events_added: int = 0
    message: str = ""


def _sig(e) -> tuple:
    get = (lambda k: e.get(k)) if isinstance(e, dict) else (lambda k: getattr(e, k))
    t = get("event_datetime")
    return (t.replace(microsecond=0) if t else None,
            round(get("dap"), 9) if get("dap") is not None else None,
            get("kvp"), round(get("mas"), 3) if get("mas") is not None else None,
            round(get("agd"), 6) if get("agd") is not None else None,
            get("laterality"))


def _source(e) -> str | None:
    return (e.extra_json or {}).get("source")


DERIVE_FAILED = "derive failed"  # in IngestedObject.message: see maintenance.repair_failed_derive


class Helpers:
    """Classifier, indication matcher and alert evaluator, built once and reused for many
    objects (each loads whole tables); rebuilt after ``ttl`` seconds so that edits to
    confirmed mappings, the vocabulary or alert rules reach a long-running worker."""

    def __init__(self, session: Session, ttl: float = 60.0):
        self.session, self.ttl = session, ttl
        self._built = float("-inf")
        self.classifier = self.matcher = self.evaluator = None

    def _fresh(self) -> None:
        now = time.monotonic()
        if now - self._built < self.ttl:
            return
        from .. import alerts, classify, indications

        self.classifier = classify.Classifier(self.session)
        try:
            self.matcher = indications.Matcher(self.session)
        except ValueError:  # vocabulary not seeded: indications.update handles it
            self.matcher = None
        self.evaluator = alerts.Evaluator(self.session)
        self._built = now

    def get(self) -> Helpers:
        self._fresh()
        return self


class Store:
    def __init__(self, session: Session, secret: bytes, institution_code: str = "DEFAULT",
                 source: str = "file"):
        self.session = session
        self.secret = secret
        self.source = source
        self.institution_id = self._institution(institution_code)
        self.stats: Counter = Counter()
        self._his_enabled: bool | None = None  # any enabled HIS server (looked up once)
        self._helpers = Helpers(session)

    # ------------------------------------------------------------------ entities

    def _institution(self, code: str) -> int:
        """Get or create; tolerate a concurrent worker creating it first."""
        inst = self.session.scalar(select(Institution).filter_by(code=code))
        if inst is not None:
            return inst.id
        try:
            with self.session.begin_nested():
                inst = Institution(code=code, name="Default", is_local=True)
                self.session.add(inst)
            return inst.id
        except IntegrityError:
            return self.session.scalar(select(Institution.id).filter_by(code=code))

    def device(self, d: dict) -> Device | None:
        if not any(d.get(k) for k in ("manufacturer", "model_name", "station_name",
                                      "device_observer_uid")):
            return None
        key = device_key(d.get("manufacturer"), d.get("model_name"), d.get("serial_number"),
                         d.get("station_name"), d.get("institution_name"),
                         d.get("department_name"), d.get("device_observer_uid"))
        dev = self.session.scalar(select(Device).filter_by(identity_key=key))
        if dev is None:
            # a new equipment identity: unknown origin until an admin decides (external
            # by default, so outside studies never pollute local statistics)
            dev = Device(identity_key=key, institution_id=self.institution_id, is_local=False,
                         **{k: d.get(k) for k in ("manufacturer", "model_name", "serial_number",
                                                  "device_observer_uid", "station_name",
                                                  "institution_name", "department_name",
                                                  "software_versions", "gantry_id")})
            dev.display_name = display_name(d.get("station_name"), d.get("model_name"))
            self.session.add(dev)
            self.session.flush()
            self.stats["devices_new"] += 1
        return dev

    def patient(self, p: dict) -> Patient | None:
        pid = p.get("patient_id")
        if not pid:
            return None
        h = patient_key(pid)
        pat = self.session.scalar(select(Patient).filter_by(legacy_sha256=h))
        if pat is None:
            pat = Patient(legacy_sha256=h, pseudo_id=pseudonym(self.secret, h))
            self.session.add(pat)
            self.stats["patients_new"] += 1
        pat.patient_id = pat.patient_id or pid
        pat.issuer = pat.issuer or p.get("issuer")
        pat.name = pat.name or p.get("name")
        demographics.reconcile(pat, dicom_sex=p.get("sex"), dicom_birth=p.get("birth_date"))
        self.session.flush()
        return pat

    # ------------------------------------------------------------------ main entry

    def store(self, obj: ParsedObject) -> Result:
        seen = self.session.scalar(select(IngestedObject)
                                   .filter_by(sop_instance_uid=obj.sop_instance_uid[:64]))
        # an object ignored before may be usable now (a parser learnt it, e.g. the OCR of a
        # dose page): only a second "ignored" verdict is a duplicate
        improved = obj.kind == "ct_header" and any(
            (e.get("extra_json") or {}).get("dlp_method") in ("z_extent", "legacy_ge_nctdiw")
            for e in obj.events)
        if seen is not None and not (seen.status == "imported" and improved) and (
                seen.status in ("imported", "superseded")
                or (seen.status == "ignored" and obj.kind == "ignored")):
            return self._count(Result("duplicate", seen.study_id, message="already processed"))
        if obj.kind == "ignored":
            self._record(obj, None, "ignored", obj.reason, seen)
            return self._count(Result("ignored", message=obj.reason))
        if not obj.study.get("study_uid"):
            self._record(obj, None, "failed", "no Study Instance UID", seen)
            return self._count(Result("failed", message="no Study Instance UID"))
        if obj.kind == "staff":  # names only: fill the blanks of a study we already have
            study = self.session.scalar(select(Study).filter_by(study_uid=obj.study["study_uid"]))
            if study is None:
                self._record(obj, None, "ignored", "staff header for an unknown study", seen)
                return self._count(Result("ignored", message="study not known"))
            for f in ("operator_name", "performing_physician"):
                if not getattr(study, f) and obj.study.get(f):
                    setattr(study, f, obj.study[f])
            self.session.flush()
            self._record(obj, study.id, "imported", "staff names", seen)
            self.stats["staff_headers"] += 1
            return self._count(Result("imported", study.id))

        device = self.device(obj.device)
        patient = self.patient(obj.patient)
        study, new_study = self._study(obj, device, patient)

        existing = list(self.session.scalars(select(Event).where(Event.study_id == study.id)))
        projection = obj.kind in ("proj_rdsr", "header")
        if obj.kind in ("header", "ct_header") and any(_source(e) in ("rdsr", "ocr") for e in existing):
            self._record(obj, study.id, "superseded", "study has a dose report", seen)
            return self._count(Result("superseded", study.id,
                                      message="a dose report already covers this study"))
        if obj.kind in ("proj_rdsr", "ct_rdsr") or (obj.kind == "dose_page" and obj.events):
            # a CT dose report (or the scanner's dose page) replaces the estimated CT header
            # events, not the radiopharmaceutical administrations of a PET/CT (also from headers)
            stale = [e for e in existing if _source(e) == "header"
                     and (obj.kind == "proj_rdsr" or e.event_type != "radiopharmaceutical")]
            for e in stale:
                self.session.delete(e)
            if stale:
                self.stats["header_events_replaced"] += len(stale)
                existing = [e for e in existing if e not in stale]

        # a header estimate made from the z range replaces one made from count x thickness
        by_uid = {e.event_uid: e for e in existing if e.event_uid}
        for ev in obj.events:
            old = by_uid.get(ev.get("event_uid"))
            if obj.kind == "ct_header" and old is not None and _source(old) == "header":
                new_m = (ev.get("extra_json") or {}).get("dlp_method")
                old_m = (old.extra_json or {}).get("dlp_method", "count_x_thickness")
                if (new_m == "z_extent" and old_m == "count_x_thickness") or (
                        new_m == "legacy_ge_nctdiw" and old_m != "legacy_ge_nctdiw"):
                    for k in ("ctdivol", "dlp", "scan_length_mm", "ct_phantom_cm", "extra_json"):
                        if k in ev:
                            setattr(old, k, ev[k])
                    self.stats["header_estimates_improved"] += 1
        uids = {e.event_uid for e in existing if e.event_uid}
        sigs = {_sig(e) for e in existing} if projection else set()
        added = 0
        for ev in obj.events:
            ev = zero_as_missing(ev)
            if ev.get("event_uid") and ev["event_uid"] in uids:
                continue
            # mammograms carry a glandular dose instead of a DAP
            if projection and (ev.get("dap") is not None or ev.get("agd") is not None) \
                    and _sig(ev) in sigs:
                self.stats["duplicate_exposures"] += 1
                continue
            self.session.add(Event(study_id=study.id, **ev))
            uids.add(ev.get("event_uid"))
            sigs.add(_sig(ev))
            added += 1
        self.session.flush()
        update_study_totals(self.session, study)
        if any(ev.get("event_type") == "radiopharmaceutical" for ev in obj.events):
            settle_administrations(self.session, study)
        derived = self._classify(study.id)
        if device is not None:
            _touch_device(device, study.study_datetime, new_study)
        # a failed derive step is marked on the object: maintenance.repair_failed_derive
        # retries it (and the status shows it), the study is not silently left bare
        self._record(obj, study.id, "imported",
                     f"{added} event(s)" + ("" if derived else f"; {DERIVE_FAILED} (try 0)"),
                     seen)
        self.stats["events_added"] += added
        self.stats["studies_new" if new_study else "studies_updated"] += 1
        return self._count(Result("imported", study.id, added))

    # ------------------------------------------------------------------ helpers

    def _study(self, obj: ParsedObject, device, patient) -> tuple[Study, bool]:
        s = obj.study
        study = self.session.scalar(select(Study).filter_by(study_uid=s["study_uid"]))
        new = study is None
        if new:
            try:  # another process (receiver / qr) may create the same study concurrently
                with self.session.begin_nested():
                    study = Study(study_uid=s["study_uid"], modality=s.get("modality") or "OT",
                                  institution_id=self.institution_id, source_kind=obj.kind,
                                  n_events=0)
                    self.session.add(study)
            except IntegrityError:
                # a locking read sees the row the other transaction committed
                study = self.session.scalar(select(Study).filter_by(study_uid=s["study_uid"])
                                            .with_for_update())
                if study is None:
                    raise
                new = False
        for f in STUDY_FIELDS:
            if getattr(study, f) is None and s.get(f) is not None:
                setattr(study, f, s[f])
        if study.device_id is None and device is not None:
            study.device_id = device.id
        if study.patient_id is None and patient is not None:
            study.patient_id = patient.id
        birth = patient.birth_date if patient is not None else None
        if not demographics.exact_age(study, birth) and study.age_days is None:
            days, precision = age_from(s, obj.patient)
            if days is not None:
                study.age_days, study.age_precision = days, AgePrecision(precision)
        if s.get("weight_kg") and (study.weight_kg is None
                                   or study.weight_source == SizeSource.estimated_reference):
            # a reported weight replaces an estimate (it is checked again in _classify)
            study.weight_kg, study.weight_source = s["weight_kg"], SizeSource.dicom_unknown
            study.weight_reported_kg = None
        if study.height_m is None and s.get("height_m"):
            study.height_m, study.height_source = s["height_m"], SizeSource.dicom_unknown
        if obj.kind in ("ct_rdsr", "proj_rdsr", "ct_header", "dose_page") \
                and study.modality in ("SR", "OT", None):
            study.modality = s.get("modality") or study.modality
        if obj.kind == "ct_rdsr" and study.source_kind in ("ct_header", "dose_page"):
            study.source_kind = "ct_rdsr"  # the dose report replaced the image headers
        if obj.kind == "dose_page" and obj.events and study.source_kind == "ct_header":
            study.source_kind = "dose_page"  # the scanner's own values replaced the estimates
        if obj.kind == "nm_header" and study.modality in ("CT", "SR", "OT", None) \
                and s.get("modality") in ("PT", "NM"):
            # a PET/CT or SPECT/CT whose CT dose report came first (hybrid.py)
            study.modality = s["modality"]
        self.session.flush()
        if new and self._his_wanted(device) and _recent(study.study_datetime):
            self.session.add(HisLookup(study_id=study.id, status="pending", attempts=0))
            self.session.flush()
        return study, new

    def _his_wanted(self, device) -> bool:
        """Queue a HIS lookup for a new study (done later by `dozimetrix his run`)."""
        if self._his_enabled is None:
            self._his_enabled = self.session.scalar(
                select(HisServer.id).where(HisServer.enabled.is_(True)).limit(1)) is not None
        return self._his_enabled and (device is None or device.his_policy != "none")

    def _classify(self, study_id: int) -> bool:
        """Exam codes and alerts for the touched study; never blocks an import. False when
        a step failed (the study then lacks codes, weight estimates, skin dose, alerts)."""
        import logging

        from .. import classify

        try:
            with self.session.begin_nested():
                h = self._helpers.get()
                classify.run(self.session, [study_id], classifier=h.classifier)
                derive(self.session, [study_id], h)
        except Exception:  # noqa: BLE001 – never blocks an import; marked for a retry
            logging.getLogger(__name__).exception("classification of study %s failed",
                                                  study_id)
            self.stats["classify_failed"] += 1
            return False
        return True

    def _record(self, obj, study_id, status, message, seen) -> None:
        row = seen or IngestedObject(sop_instance_uid=obj.sop_instance_uid[:64],
                                     source=self.source)
        row.sop_class_uid = (obj.sop_class_uid or "")[:64] or None
        row.study_id, row.status, row.message = study_id, status, (message or "")[:1024]
        self.session.add(row)
        self.session.flush()

    def _count(self, r: Result) -> Result:
        self.stats[r.status] += 1
        return r


HIS_NEW_DAYS = 7  # older exams (PACS or legacy backfill) are history: CNP + estimates only


def _recent(when: dt.datetime | None) -> bool:
    return when is None or when >= dt.datetime.now() - dt.timedelta(days=HIS_NEW_DAYS)


def derive(session: Session, study_ids: list[int], helpers: Helpers | None = None) -> None:
    """Values derived from weight, age and doses: weight estimates, DRL exclusions, skin
    dose, alerts. Run after anything changes them (ingest, HIS enrichment). ``helpers``
    reuses prebuilt lookup tables when called per object."""
    from .. import alerts, growth, indications, skindose, ssde

    growth.estimate_weights(session, study_ids)
    ssde.update(session, study_ids)
    if helpers is None or helpers.matcher is not None:  # no vocabulary: nothing to code
        indications.update(session, study_ids, matcher=helpers.matcher if helpers else None)
    growth.apply_exclusions(session, study_ids)
    skindose.update_psd(session, study_ids)
    alerts.evaluate(session, study_ids, evaluator=helpers.evaluator if helpers else None)


# Factory defaults that name no station (Toshiba writes ID_STATION on every scanner).
PLACEHOLDER_STATIONS = {"id_station", "station", "unknown", "none", "n/a"}


def display_name(station: str | None, model: str | None) -> str | None:
    """A device's label: the station name unless it is a vendor placeholder, then the model."""
    if station and station.strip().casefold() not in PLACEHOLDER_STATIONS:
        return station
    return model or station


def _touch_device(device: Device, when: dt.datetime | None, new_study: bool) -> None:
    if new_study:
        device.n_studies = (device.n_studies or 0) + 1
    if when:
        if device.last_study_at is None or when > device.last_study_at:
            device.last_study_at = when
        if device.first_study_at is None or when < device.first_study_at:
            device.first_study_at = when


# A dose or distance of exactly 0 for an exposure that happened is not a measurement: some
# units write 0 when the DAP meter is absent, off or not calibrated. Stored as missing, so
# it does not pull medians down; the report's value is kept in extra_json.
ZERO_IS_MISSING = ("dap", "rp_dose", "sid_mm")


def zero_as_missing(ev: dict) -> dict:
    zeros = [k for k in ZERO_IS_MISSING if ev.get(k) == 0]
    if not zeros:
        return ev
    return {**ev, **dict.fromkeys(zeros), "extra_json": {**(ev.get("extra_json") or {}),
                                                         "reported_zero": zeros}}


def update_study_totals(session: Session, study: Study) -> None:
    events = list(session.scalars(select(Event).where(Event.study_id == study.id)))
    dlp = [e.dlp for e in events if e.dlp is not None]
    dap = [e.dap for e in events if e.dap is not None]
    rp = [e.rp_dose for e in events if e.rp_dose is not None]
    ft = [e.fluoro_time_s for e in events if e.fluoro_time_s is not None]
    study.n_events = len(events)
    study.total_dlp = sum(dlp) if dlp else None
    study.total_dap = sum(dap) if dap else None
    study.total_rp_dose = sum(rp) if rp else None
    study.total_fluoro_time = sum(ft) if ft else None
    fl = [e.dap for e in events if e.dap is not None and e.event_type == "fluoro"]
    ac = [e.dap for e in events if e.dap is not None and e.event_type != "fluoro"]
    study.total_fluoro_dap = sum(fl) if fl else None
    study.total_acq_dap = sum(ac) if ac else None
    for side, col in (("L", "agd_left"), ("R", "agd_right")):
        v = [e.agd for e in events if e.agd is not None and e.laterality == side]
        setattr(study, col, sum(v) if v else None)
    study.n_spiral = sum(1 for e in events if e.event_type == "spiral")
    study.n_axial = sum(1 for e in events if e.event_type == "axial")
    study.n_localizer = sum(1 for e in events if e.is_localizer)
    cts = [e.ctdivol for e in events if e.ctdivol is not None and not e.is_localizer]
    study.ctdivol_max = max(cts) if cts else None
    mbq = [(e.extra_json or {}).get("activity_mbq") for e in events
           if e.event_type == "radiopharmaceutical"
           and not (e.extra_json or {}).get("same_administration_as")]
    mbq = [v for v in mbq if v]
    study.admin_activity_mbq = sum(mbq) if mbq else None
    session.flush()


SAME_ADMIN_WINDOW = dt.timedelta(seconds=59)  # start times are recorded to the minute
SAME_ADMIN_ACTIVITY = 0.01  # relative difference


def _same_administration(a: Event, b: Event) -> bool:
    """Same agent, start time to the minute, same activity (both unknown, or within 1 %).
    A rest / stress pair or a second tracer differ in time, agent or activity."""
    from ..classify import normalise

    if a.event_datetime is None or b.event_datetime is None:
        return False
    if abs(a.event_datetime - b.event_datetime) > SAME_ADMIN_WINDOW:
        return False
    if normalise(a.protocol) != normalise(b.protocol) or \
            normalise(a.comment) != normalise(b.comment):
        return False
    x, y = ((e.extra_json or {}).get("activity_mbq") for e in (a, b))
    if x is None or y is None:
        return x is None and y is None
    return abs(x - y) <= SAME_ADMIN_ACTIVITY * max(x, y)


def settle_administrations(session: Session, study: Study) -> list[int]:
    """Mark administrations of ``study`` that another study of the same patient also
    holds: the earliest study (then the lowest id) keeps them, the others' copies get
    ``same_administration_as`` = its id. Returns the ids of the studies changed."""
    if study.patient_id is None:
        return []
    own = list(session.scalars(select(Event).where(
        Event.study_id == study.id, Event.event_type == "radiopharmaceutical",
        Event.event_datetime.is_not(None))))
    if not own:
        return []
    lo = min(e.event_datetime for e in own) - SAME_ADMIN_WINDOW
    hi = max(e.event_datetime for e in own) + SAME_ADMIN_WINDOW
    rows = session.execute(
        select(Event, Study).join(Study, Study.id == Event.study_id)
        .where(Study.patient_id == study.patient_id, Study.id != study.id,
               Event.event_type == "radiopharmaceutical",
               Event.event_datetime.between(lo, hi))).all()
    changed: set[int] = set()

    def order(s: Study) -> tuple:
        return (s.study_datetime or dt.datetime.max, s.id)

    def mark(e: Event, owner: int | None) -> None:
        x = dict(e.extra_json or {})
        if x.get("same_administration_as") == owner:
            return
        if owner is None:
            x.pop("same_administration_as", None)
        else:
            x["same_administration_as"] = owner
        e.extra_json = x
        changed.add(e.study_id)

    for e in own:
        copies = [(o, s) for o, s in rows if _same_administration(e, o)]
        if not copies:
            mark(e, None)
            continue
        first = min([study, *(s for _o, s in copies)], key=order)
        for ev, s in [(e, study), *copies]:
            mark(ev, None if s.id == first.id else first.id)
    session.flush()
    for sid in changed:
        update_study_totals(session, session.get(Study, sid))
    return sorted(changed)
