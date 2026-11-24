"""Import an OpenREM CSV dump (see dump.py) into Dozimetrix.

* Patients are keyed by sha256(utf8(patient_id)) – OpenREM's own hashing recipe – so
  hashed historical records and clear-ID records of the same patient merge automatically.
* Devices are keyed by device observer UID > manufacturer + serial > normalised tuple,
  so case variants of the same station collapse into one device.
* Studies are merged by Study Instance UID; events are unique per (study, event UID).
* Units are converted to Dozimetrix storage units (DAP Gy·m² -> Gy·cm², µAs -> mAs, ms -> s).
* Re-running skips studies whose UID already exists.
* Exact duplicate exposures inside a study (same time to the second, same DAP, kVp, mAs and
  protocol – e.g. one exposure stored both as a CR and a DX object) are removed, keeping
  the non-CR copy. Every removal is written to an audit file (dedup-report.csv.gz).
* Known OpenREM value errors (fixes.py, e.g. Canon Ultimax Ka,r stored 1000x too high) are
  corrected after import, audited and marked on each corrected event.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import gzip
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.orm import Session

from ...db.models import (
    AgePrecision,
    Device,
    Event,
    IngestedObject,
    Institution,
    Patient,
    SizeSource,
    Study,
)
from ...ingest.identity import device_key, patient_key
from ...maintenance import recompute_all
from ...patient_age import parse_dicom_age

BATCH = 5000
DAP_GYM2_TO_GYCM2 = 1e4

CT_TYPES = {
    "spiral acquisition": "spiral",
    "sequenced acquisition": "axial",
    "constant angle acquisition": "localizer",
    "stationary acquisition": "stationary",
    "free acquisition": "free",
    "cone beam acquisition": "cone_beam",
}
PROJ_TYPES = {
    "fluoroscopy": "fluoro",
    "stationary acquisition": "acquisition",
    "stepping acquisition": "acquisition",
    "rotational acquisition": "acquisition",
}


# ----------------------------------------------------------------------------- helpers


def rows(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        yield from csv.DictReader(fh)


def num(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def boolean(v: str | None) -> bool:
    return v in ("t", "true", "True", "1")


def s(v: str | None, maxlen: int = 255) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v[:maxlen] or None


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_date(v: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(v) if v else None
    except ValueError:
        return None


def parse_dt(date: str | None, time: str | None) -> dt.datetime | None:
    d = parse_date(date)
    if d is None:
        return None
    t = dt.time()
    if time:
        with contextlib.suppress(ValueError):
            t = dt.time.fromisoformat(time.split(".")[0])
    return dt.datetime.combine(d, t)


def parse_ts(v: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def device_identity(r: dict) -> str:
    return device_key(r.get("manufacturer"), r.get("manufacturer_model_name"),
                      r.get("device_serial_number"), r.get("station_name"),
                      r.get("institution_name"), r.get("institutional_department_name"),
                      r.get("device_observer_uid"))


def filters(v: str | None) -> tuple[float | None, float | None]:
    """'Copper:0.1;Aluminum:1.0' -> (cu_mm, al_mm)."""
    cu = al = None
    for part in (v or "").split(";"):
        if ":" not in part:
            continue
        mat, thick = part.rsplit(":", 1)
        t = num(thick)
        m = mat.casefold()
        if "copper" in m or m.strip() == "cu":
            cu = (cu or 0) + (t or 0)
        elif "alumin" in m or m.strip() == "al":
            al = (al or 0) + (t or 0)
    return cu, al


def plane(v: str | None) -> str | None:
    v = (v or "").casefold()
    if "plane b" in v:
        return "B"
    if "plane a" in v or "single" in v:
        return "A"
    return None


def laterality(v: str | None) -> str | None:
    v = (v or "").casefold()
    return {"right": "R", "left": "L", "both left and right": "B", "bilateral": "B"}.get(v)


# ----------------------------------------------------------------------------- importer


def phantom_cm(meaning: str | None) -> int | None:
    """OpenREM's CTDIw phantom (a code meaning) -> 16 / 32 cm. OpenREM writes "IEC Head
    Dosimetry Phantom" / "IEC Body Dosimetry Phantom", without the size."""
    m = (meaning or "").casefold()
    if "head" in m or "16" in m:
        return 16
    if "body" in m or "32" in m:
        return 32
    return None

@dataclass
class Stats:
    studies_read: int = 0
    studies_new: int = 0
    studies_merged: int = 0
    studies_skipped_existing: int = 0
    patients: int = 0
    patients_linked_hash_to_clear: int = 0
    devices: int = 0
    ct_events: int = 0
    proj_events: int = 0
    events_duplicate: int = 0
    events_orphan: int = 0
    exposures_deduplicated: int = 0
    dap_deduplicated_gycm2: float = 0.0
    studies_modality_cr_to_dx: int = 0
    objects: int = 0
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [f"{k}: {v}" for k, v in self.__dict__.items() if k != "notes"] + self.notes


class OpenREMImporter:
    def __init__(self, session: Session, dump_dir: Path, institution_name: str = "Default",
                 secret: bytes | None = None):
        self.session = session
        self.secret = secret
        self.conn = session.connection()
        self.dir = dump_dir
        self.stats = Stats()
        self.institution_id = self._institution(institution_name)
        self.study_ids: dict[str, int] = {}  # openrem study pk -> dozimetrix study id
        self.orem_modality: dict[str, str] = {}  # openrem study pk -> modality_type
        self.study_modality: dict[int, str] = {}
        self.modality_fix: dict[int, str] = {}
        self.uid_to_study: dict[str, int] = {}
        self.patient_by_hash: dict[str, int] = {}
        self.patient_has_clear: set[int] = set()
        self.device_by_key: dict[str, int] = {}
        self.display_votes: dict[int, Counter] = defaultdict(Counter)
        self.event_uids: dict[int, set[str]] = defaultdict(set)
        self.totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._next = {m: (self.session.scalar(select(func.max(m.id))) or 0) + 1
                      for m in (Study, Patient, Device, Event)}

    # -- infrastructure

    def _institution(self, name: str) -> int:
        inst = self.session.scalar(select(Institution).filter_by(code="DEFAULT"))
        if inst is None:
            inst = Institution(code="DEFAULT", name=name, is_local=True)
            self.session.add(inst)
            self.session.flush()
        return inst.id

    def _id(self, model) -> int:
        i = self._next[model]
        self._next[model] = i + 1
        return i

    def _flush(self, model, batch: list[dict]) -> None:
        if batch:
            self.conn.execute(insert(model.__table__), batch)
            batch.clear()

    # -- pass 1: studies, patients, devices

    def _load_existing(self) -> None:
        for sid, uid in self.session.execute(select(Study.id, Study.study_uid)):
            self.uid_to_study[uid] = sid
        for pid, h, clear in self.session.execute(
                select(Patient.id, Patient.legacy_sha256, Patient.patient_id)):
            if h:
                self.patient_by_hash[h] = pid
            if clear:
                self.patient_has_clear.add(pid)
        for did, key in self.session.execute(select(Device.id, Device.identity_key)):
            self.device_by_key[key] = did

    def import_studies(self) -> None:
        self._load_existing()
        preexisting = set(self.uid_to_study)
        studies, patients, devices = [], [], []
        patient_updates: dict[int, dict] = {}
        for r in rows(self.dir / "studies.csv.gz"):
            self.stats.studies_read += 1
            uid = s(r["study_instance_uid"], 64)
            if not uid:
                self.stats.notes.append(f"study without UID skipped (openrem id {r['orem_id']})")
                continue
            if uid in preexisting:
                self.stats.studies_skipped_existing += 1
                continue
            modality = s(r.get("modality_type"), 16) or "OT"
            self.orem_modality[r["orem_id"]] = modality
            if uid in self.uid_to_study:  # duplicate/continued study in OpenREM -> merge
                sid = self.uid_to_study[uid]
                self.study_ids[r["orem_id"]] = sid
                self.stats.studies_merged += 1
                if {self.study_modality.get(sid), modality} == {"CR", "DX"}:
                    self.modality_fix[sid] = "DX"  # Siemens CR+DX copies of one study
                continue

            patient_id = self._patient(r, patients, patient_updates)
            device_id = self._device(r, devices)
            sid = self._id(Study)
            self.uid_to_study[uid] = sid
            self.study_modality[sid] = modality
            self.study_ids[r["orem_id"]] = sid
            studies.append(self._study_row(sid, uid, r, patient_id, device_id))
            self.stats.studies_new += 1
            if len(studies) >= BATCH:
                self._flush(Patient, patients)
                self._flush(Device, devices)
                self._flush(Study, studies)
        self._flush(Patient, patients)
        self._flush(Device, devices)
        self._flush(Study, studies)
        for sid, mod in self.modality_fix.items():
            self.conn.execute(update(Study).where(Study.id == sid).values(modality=mod))
        self.stats.studies_modality_cr_to_dx = len(self.modality_fix)
        for pid, values in patient_updates.items():
            self.conn.execute(update(Patient).where(Patient.id == pid).values(**values))
        for did, votes in self.display_votes.items():
            name = votes.most_common(1)[0][0]
            self.conn.execute(update(Device).where(Device.id == did).values(display_name=name))

    def _patient(self, r: dict, batch: list[dict], updates: dict[int, dict]) -> int | None:
        raw = s(r.get("patient_id"), 64)
        if not raw:
            return None
        hashed = boolean(r.get("id_hashed"))
        h = raw.lower() if hashed else patient_key(raw)
        name = None if boolean(r.get("name_hashed")) else s(r.get("patient_name"))
        clear = None if hashed else raw
        pid = self.patient_by_hash.get(h)
        if pid is None:
            pid = self._id(Patient)
            self.patient_by_hash[h] = pid
            batch.append({
                "id": pid, "patient_id": clear, "name": name, "legacy_sha256": h,
                "birth_date": parse_date(r.get("patient_birth_date")),
                "sex": (s(r.get("patient_sex"), 1) or None),
                "is_test": bool(s(r.get("not_patient_indicator"))),
                "test_reason": s(r.get("not_patient_indicator")),
            })
            self.stats.patients += 1
            if clear:
                self.patient_has_clear.add(pid)
        elif clear and pid not in self.patient_has_clear:
            # first clear-ID record for a patient known so far only by hash
            self.patient_has_clear.add(pid)
            updates[pid] = {"patient_id": clear, "name": name}
            self.stats.patients_linked_hash_to_clear += 1
        return pid

    def _device(self, r: dict, batch: list[dict]) -> int | None:
        if not any(r.get(k) for k in ("manufacturer", "manufacturer_model_name", "station_name")):
            return None
        ikey = device_identity(r)
        did = self.device_by_key.get(ikey)
        if did is None:
            did = self._id(Device)
            self.device_by_key[ikey] = did
            batch.append({
                "id": did, "institution_id": self.institution_id,
                "manufacturer": s(r.get("manufacturer"), 128),
                "model_name": s(r.get("manufacturer_model_name"), 128),
                "serial_number": s(r.get("device_serial_number"), 64),
                "device_observer_uid": s(r.get("device_observer_uid"), 64),
                "station_name": s(r.get("station_name"), 64),
                "institution_name": s(r.get("institution_name")),
                "department_name": s(r.get("institutional_department_name")),
                "software_versions": s(r.get("software_versions")),
                "gantry_id": s(r.get("gantry_id"), 64),
                "identity_key": ikey,
                "modality_override": s(r.get("user_defined_modality"), 16),
                "is_local": True,
            })
            self.stats.devices += 1
        if r.get("display_name"):
            self.display_votes[did][r["display_name"].strip()] += 1
        return did

    def _study_row(self, sid: int, uid: str, r: dict, patient_id, device_id) -> dict:
        study_dt = parse_dt(r.get("study_date"), r.get("study_time"))
        age_days, precision = self._age(r, study_dt)
        weight = num(r.get("patient_weight"))
        height = num(r.get("patient_size"))
        return {
            "id": sid, "study_uid": uid,
            "accession": s(r.get("accession_number"), 64),
            "accession_is_hashed": boolean(r.get("accession_hashed")),
            "patient_id": patient_id, "device_id": device_id,
            "institution_id": self.institution_id,
            "modality": s(r.get("modality_type"), 16) or "OT",
            "study_datetime": study_dt,
            "study_description": s(r.get("study_description")),
            "requested_procedure": s(r.get("requested_procedure_code_meaning")),
            "procedure_name": s(r.get("procedure_code_meaning")),
            "performing_physician": s(r.get("performing_physician_name")),
            "operator_name": s(r.get("operator_name")),
            "referring_physician": s(r.get("referring_physician_name")),
            "age_days": age_days, "age_precision": precision,
            "weight_kg": weight if weight and weight > 0 else None,
            "weight_source": SizeSource.imported if weight and weight > 0 else None,
            "height_m": height if height and height > 0 else None,
            "height_source": SizeSource.imported if height and height > 0 else None,
            "n_events": 0, "source_kind": "openrem",
            "source_json": {"openrem_id": int(r["orem_id"])},
        }

    @staticmethod
    def _age(r: dict, study_dt: dt.datetime | None) -> tuple[int | None, AgePrecision]:
        birth = parse_date(r.get("patient_birth_date"))
        if birth and birth.year < 1901:  # an "unknown" placeholder (1753-01-01), not a birth date
            birth = None
        if birth and study_dt and study_dt.date() >= birth:
            return (study_dt.date() - birth).days, AgePrecision.exact
        parsed = parse_dicom_age(r.get("patient_age"))
        if parsed:
            amount, unit = parsed
            factor = {"D": 1, "W": 7, "M": 30.4375, "Y": 365.25}[unit]
            return int(amount * factor), AgePrecision.dicom_age
        dec = num(r.get("patient_age_decimal"))
        if dec is not None and dec >= 0:
            return int(dec * 365.25), AgePrecision.dicom_age
        return None, AgePrecision.unknown

    # -- pass 2/3: events

    def _event_base(self, r: dict) -> tuple[int, dict] | None:
        sid = self.study_ids.get(r["orem_study_id"])
        if sid is None:
            self.stats.events_orphan += 1
            return None
        uid = s(r.get("irradiation_event_uid"), 64)
        if uid:
            if uid in self.event_uids[sid]:
                self.stats.events_duplicate += 1
                return None
            self.event_uids[sid].add(uid)
        return sid, {
            "id": self._id(Event), "study_id": sid, "event_uid": uid,
            "event_datetime": parse_ts(r.get("started")),
            "protocol": s(r.get("acquisition_protocol")),
            "comment": s(r.get("comment") or r.get("series_description")),
        }

    def import_ct_events(self) -> None:
        batch = []
        for r in rows(self.dir / "ct_events.csv.gz"):
            base = self._event_base(r)
            if base is None:
                continue
            sid, e = base
            etype = CT_TYPES.get((r.get("acquisition_type") or "").casefold())
            phantom = r.get("phantom") or ""
            ms = num(r.get("exposure_time"))
            e.update(
                event_type=etype, anatomy=s(r.get("target_region"), 128),
                ctdivol=num(r.get("mean_ctdivol")), dlp=num(r.get("dlp")),
                ct_phantom_cm=phantom_cm(phantom),
                scan_length_mm=num(r.get("scanning_length")),
                pitch=num(r.get("pitch_factor")),
                collimation_mm=num(r.get("nominal_total_collimation_width")),
                rotation_time_s=num(r.get("rotation_time")),
                tcm=s(r.get("xray_modulation_type"), 64),
                is_localizer=etype == "localizer",
                dose_check_alert=boolean(r.get("dose_check_alert")),
                kvp=num(r.get("kvp")), ma=num(r.get("ma")), ma_max=num(r.get("ma_max")),
                exposure_time_s=ms,  # OpenREM CT exposure_time is already in seconds
            )
            t = self.totals[sid]
            t["n"] += 1
            if e["dlp"] is not None:
                t["dlp"] += e["dlp"]
            batch.append(e)
            self.stats.ct_events += 1
            if len(batch) >= BATCH:
                self._flush(Event, batch)
        self._flush(Event, batch)

    def import_proj_events(self) -> None:
        batch = []
        for r in rows(self.dir / "proj_events.csv.gz"):
            base = self._event_base(r)
            if base is None:
                continue
            sid, e = base
            etype = PROJ_TYPES.get((r.get("event_type") or "").casefold(),
                                   s(r.get("event_type"), 32))
            dap_gym2 = num(r.get("dose_area_product"))
            dap = dap_gym2 * DAP_GYM2_TO_GYCM2 if dap_gym2 is not None else None
            cu, al = filters(r.get("filters"))
            exp_uas = num(r.get("exposure_uas"))
            exp_ms = num(r.get("exposure_time"))
            duration = num(r.get("irradiation_duration"))
            e.update(
                event_type=etype if etype else None,
                plane=plane(r.get("plane")), anatomy=s(r.get("anatomy"), 128),
                laterality=laterality(r.get("laterality")), view=s(r.get("view"), 64),
                dap=dap, rp_dose=num(r.get("dose_rp")),
                kvp=num(r.get("kvp")),
                ma=num(r.get("tube_current")) or num(r.get("average_xray_tube_current")),
                mas=exp_uas / 1000 if exp_uas is not None else None,
                exposure_time_s=exp_ms / 1000 if exp_ms is not None else None,
                pulse_rate=num(r.get("pulse_rate")),
                pulse_width_ms=num(r.get("pulse_width")),
                n_pulses=int(num(r.get("number_of_pulses"))) if num(r.get("number_of_pulses"))
                else None,
                fluoro_time_s=duration if etype == "fluoro" else None,
                filter_cu_mm=cu, filter_al_mm=al, grid=s(r.get("grid"), 64),
                sid_mm=num(r.get("distance_source_to_detector")),
                ssd_mm=num(r.get("distance_source_to_entrance_surface")),
                field_w_mm=num(r.get("collimated_field_width")),
                field_h_mm=num(r.get("collimated_field_height")),
                primary_angle=num(r.get("positioner_primary_angle")),
                secondary_angle=num(r.get("positioner_secondary_angle")),
                exposure_index=num(r.get("exposure_index")),
                target_exposure_index=num(r.get("target_exposure_index")),
                deviation_index=num(r.get("deviation_index")),
                ei_vendor_value=num(r.get("relative_xray_exposure")),
                ei_vendor_unit=s(r.get("relative_exposure_unit"), 32),
                agd=num(r.get("average_glandular_dose")),
                compression_thickness_mm=num(r.get("compression_thickness")),
                compression_force_n=num(r.get("compression_force")),
                target_material=s(r.get("anode_target"), 32),
                exposure_control_mode=s(r.get("exposure_control_mode"), 64),
                extra_json={"openrem_modality": self.orem_modality.get(r["orem_study_id"])},
            )
            t = self.totals[sid]
            t["n"] += 1
            if dap is not None:
                t["dap"] += dap
                t["fluoro_dap" if etype == "fluoro" else "acq_dap"] += dap
            if e["rp_dose"] is not None:
                t["rp"] += e["rp_dose"]
            if e["fluoro_time_s"] is not None:
                t["fluoro_time"] += e["fluoro_time_s"]
            if e["agd"] is not None and e["laterality"] in ("L", "R"):
                t["agd_" + e["laterality"]] += e["agd"]
            batch.append(e)
            self.stats.proj_events += 1
            if len(batch) >= BATCH:
                self._flush(Event, batch)
        self._flush(Event, batch)

    # -- pass 4: processed objects, totals

    def import_objects(self) -> None:
        seen = set(self.session.scalars(select(IngestedObject.sop_instance_uid)))
        batch = []
        for r in rows(self.dir / "object_uids.csv.gz"):
            uid = s(r.get("sop_instance_uid"), 64)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            batch.append({"sop_instance_uid": uid, "source": "openrem", "status": "imported",
                          "study_id": self.study_ids.get(r["orem_study_id"])})
            self.stats.objects += 1
            if len(batch) >= BATCH:
                self._flush(IngestedObject, batch)
        self._flush(IngestedObject, batch)

    def deduplicate_exposures(self) -> None:
        """Remove exact duplicate exposures within a study, keeping the non-CR copy."""
        a, b = Event.__table__.alias("a"), Event.__table__.alias("b")

        def same(col):
            return or_(a.c[col] == b.c[col], and_(a.c[col].is_(None), b.c[col].is_(None)))

        pairs = select(a.c.id, b.c.id).join(b, and_(
            b.c.study_id == a.c.study_id, b.c.id > a.c.id,
            a.c.event_datetime == b.c.event_datetime,
            func.abs(a.c.dap - b.c.dap) < 1e-12, same("kvp"), same("mas"), same("protocol"),
        )).where(a.c.dap.is_not(None), a.c.event_datetime.is_not(None))
        ids = {i for pair in self.conn.execute(pairs) for i in pair}
        if not ids:
            return
        ev = Event.__table__
        st = Study.__table__
        cols = [ev.c.id, ev.c.study_id, ev.c.event_uid, ev.c.event_datetime, ev.c.dap,
                ev.c.kvp, ev.c.mas, ev.c.protocol, ev.c.extra_json, ev.c.event_type,
                ev.c.rp_dose, ev.c.fluoro_time_s, ev.c.agd, ev.c.laterality,
                st.c.study_uid, st.c.study_datetime]
        groups: dict[tuple, list] = defaultdict(list)
        id_list = sorted(ids)
        for i in range(0, len(id_list), BATCH):
            q = select(*cols).join(st, st.c.id == ev.c.study_id).where(
                ev.c.id.in_(id_list[i:i + BATCH]))
            for row in self.conn.execute(q):
                key = (row.study_id, row.event_datetime, round(row.dap, 12), row.kvp, row.mas,
                       row.protocol)
                groups[key].append(row)
        report, delete = [], []
        for members in groups.values():
            if len(members) < 2:
                continue

            def rank(r):
                src = (r.extra_json or {}).get("openrem_modality")
                return (src == "CR", r.id)  # prefer non-CR, then the first imported

            keep, *drop = sorted(members, key=rank)
            for r in drop:
                delete.append(r.id)
                self._subtract(r)
                report.append({
                    "study_uid": r.study_uid,
                    "year": r.study_datetime.year if r.study_datetime else 0,
                    "kept_event_uid": keep.event_uid, "removed_event_uid": r.event_uid,
                    "removed_source_modality": (r.extra_json or {}).get("openrem_modality"),
                    "event_datetime": r.event_datetime, "dap_gycm2": r.dap,
                })
                self.stats.dap_deduplicated_gycm2 += r.dap or 0
        for i in range(0, len(delete), BATCH):
            self.conn.execute(ev.delete().where(ev.c.id.in_(delete[i:i + BATCH])))
        self.stats.exposures_deduplicated = len(delete)
        path = self.dir / "dedup-report.csv.gz"
        with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(report[0]) if report else ["study_uid"])
            w.writeheader()
            w.writerows(report)
        path.chmod(0o600)

    def _subtract(self, r) -> None:
        t = self.totals[r.study_id]
        t["n"] -= 1
        if r.dap is not None:
            t["dap"] -= r.dap
            t["fluoro_dap" if r.event_type == "fluoro" else "acq_dap"] -= r.dap
        if r.rp_dose is not None:
            t["rp"] -= r.rp_dose
        if r.fluoro_time_s is not None:
            t["fluoro_time"] -= r.fluoro_time_s
        if r.agd is not None and r.laterality in ("L", "R"):
            t["agd_" + r.laterality] -= r.agd

    def update_totals(self) -> None:
        stmt = update(Study.__table__).where(Study.__table__.c.id == _bp("sid"))
        batch = []
        for sid, t in self.totals.items():
            batch.append({
                "sid": sid, "n_events": int(t.get("n", 0)),
                "total_dlp": t.get("dlp") if "dlp" in t else None,
                "total_dap": t.get("dap") if "dap" in t else None,
                "total_rp_dose": t.get("rp") if "rp" in t else None,
                "total_fluoro_time": t.get("fluoro_time") if "fluoro_time" in t else None,
                "total_fluoro_dap": t.get("fluoro_dap") if "fluoro_dap" in t else None,
                "total_acq_dap": t.get("acq_dap") if "acq_dap" in t else None,
                "agd_left": t.get("agd_L") if "agd_L" in t else None,
                "agd_right": t.get("agd_R") if "agd_R" in t else None,
            })
            if len(batch) >= BATCH:
                self.conn.execute(stmt, batch)
                batch.clear()
        if batch:
            self.conn.execute(stmt, batch)

    def apply_fixes(self) -> None:
        from .fixes import apply_known_fixes

        self.session.expire_all()  # rows were written through the core connection
        self.stats.notes.extend(apply_known_fixes(self.session))

    def run(self, progress=print) -> Stats:
        steps = [("studies", self.import_studies), ("ct events", self.import_ct_events),
                 ("projection events", self.import_proj_events),
                 ("processed objects", self.import_objects),
                 ("duplicate exposures", self.deduplicate_exposures),
                 ("totals", self.update_totals),
                 ("known OpenREM errors", self.apply_fixes),
                 ("derived columns", lambda: recompute_all(self.session, self.secret))]
        for label, fn in steps:
            started = dt.datetime.now()
            fn()
            progress(f"{label}: done in {(dt.datetime.now() - started).total_seconds():.0f} s")
        return self.stats


def _bp(name: str):
    from sqlalchemy import bindparam

    return bindparam(name)
