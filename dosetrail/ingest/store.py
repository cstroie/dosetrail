"""Idempotent storage of parsed DICOM objects.

Rules
* An object (SOP Instance UID) is processed once; re-sending it is a no-op ("duplicate").
* Studies are merged by Study Instance UID; events are unique per (study, event UID), so a
  cumulative RDSR (Siemens sends one after every series) only adds the new events.
* Projection dose reports are authoritative over image headers for the same study: header
  events are removed when an RDSR arrives, and later headers are not added ("superseded").
* An exact duplicate exposure (same second, DAP, kVp, mAs) is not added twice – e.g. the
  'for processing' and 'for presentation' copies of one radiograph.
* Study totals and derived columns are recomputed for the touched study.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import (
    AgePrecision,
    Device,
    Event,
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
                "referring_physician")


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
            get("kvp"), round(get("mas"), 3) if get("mas") is not None else None)


def _source(e) -> str | None:
    return (e.extra_json or {}).get("source")


class Store:
    def __init__(self, session: Session, secret: bytes, institution_code: str = "DEFAULT",
                 source: str = "file"):
        self.session = session
        self.secret = secret
        self.source = source
        self.institution_id = self._institution(institution_code)
        self.stats: Counter = Counter()

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
            dev.display_name = d.get("station_name") or d.get("model_name")
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
        pat.birth_date = pat.birth_date or p.get("birth_date")
        pat.sex = pat.sex or p.get("sex")
        self.session.flush()
        return pat

    # ------------------------------------------------------------------ main entry

    def store(self, obj: ParsedObject) -> Result:
        seen = self.session.scalar(select(IngestedObject)
                                   .filter_by(sop_instance_uid=obj.sop_instance_uid[:64]))
        if seen is not None and seen.status in ("imported", "superseded", "ignored"):
            return self._count(Result("duplicate", seen.study_id, message="already processed"))
        if obj.kind == "ignored":
            self._record(obj, None, "ignored", obj.reason, seen)
            return self._count(Result("ignored", message=obj.reason))
        if not obj.study.get("study_uid"):
            self._record(obj, None, "failed", "no Study Instance UID", seen)
            return self._count(Result("failed", message="no Study Instance UID"))

        device = self.device(obj.device)
        patient = self.patient(obj.patient)
        study, new_study = self._study(obj, device, patient)

        existing = list(self.session.scalars(select(Event).where(Event.study_id == study.id)))
        projection = obj.kind in ("proj_rdsr", "header")
        if obj.kind == "header" and any(_source(e) == "rdsr" for e in existing):
            self._record(obj, study.id, "superseded", "study has a dose report", seen)
            return self._count(Result("superseded", study.id,
                                      message="a dose report already covers this study"))
        if obj.kind == "proj_rdsr":
            stale = [e for e in existing if _source(e) == "header"]
            for e in stale:
                self.session.delete(e)
            if stale:
                self.stats["header_events_replaced"] += len(stale)
                existing = [e for e in existing if e not in stale]

        uids = {e.event_uid for e in existing if e.event_uid}
        sigs = {_sig(e) for e in existing} if projection else set()
        added = 0
        for ev in obj.events:
            if ev.get("event_uid") and ev["event_uid"] in uids:
                continue
            if projection and ev.get("dap") is not None and _sig(ev) in sigs:
                self.stats["duplicate_exposures"] += 1
                continue
            self.session.add(Event(study_id=study.id, **ev))
            uids.add(ev.get("event_uid"))
            sigs.add(_sig(ev))
            added += 1
        self.session.flush()
        update_study_totals(self.session, study)
        if device is not None:
            _touch_device(device, study.study_datetime, new_study)
        self._record(obj, study.id, "imported", f"{added} event(s)", seen)
        self.stats["events_added"] += added
        self.stats["studies_new" if new_study else "studies_updated"] += 1
        return self._count(Result("imported", study.id, added))

    # ------------------------------------------------------------------ helpers

    def _study(self, obj: ParsedObject, device, patient) -> tuple[Study, bool]:
        s = obj.study
        study = self.session.scalar(select(Study).filter_by(study_uid=s["study_uid"]))
        new = study is None
        if new:
            study = Study(study_uid=s["study_uid"], modality=s.get("modality") or "OT",
                          institution_id=self.institution_id, source_kind=obj.kind,
                          n_events=0)
            self.session.add(study)
        for f in STUDY_FIELDS:
            if getattr(study, f) is None and s.get(f) is not None:
                setattr(study, f, s[f])
        if study.device_id is None and device is not None:
            study.device_id = device.id
        if study.patient_id is None and patient is not None:
            study.patient_id = patient.id
        if study.age_days is None:
            days, precision = age_from(s, obj.patient)
            if days is not None:
                study.age_days, study.age_precision = days, AgePrecision(precision)
        if study.weight_kg is None and s.get("weight_kg"):
            study.weight_kg, study.weight_source = s["weight_kg"], SizeSource.dicom_unknown
        if study.height_m is None and s.get("height_m"):
            study.height_m, study.height_source = s["height_m"], SizeSource.dicom_unknown
        if obj.kind in ("ct_rdsr", "proj_rdsr") and study.modality in ("SR", "OT", None):
            study.modality = s.get("modality") or study.modality
        self.session.flush()
        return study, new

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


def _touch_device(device: Device, when: dt.datetime | None, new_study: bool) -> None:
    if new_study:
        device.n_studies = (device.n_studies or 0) + 1
    if when:
        if device.last_study_at is None or when > device.last_study_at:
            device.last_study_at = when
        if device.first_study_at is None or when < device.first_study_at:
            device.first_study_at = when


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
    session.flush()
