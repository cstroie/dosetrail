"""Core data model (see docs/design.md §5).

Conventions
-----------
* Storage units are fixed: DLP mGy·cm, CTDIvol mGy, DAP Gy·cm², RP dose (Ka,r) Gy,
  AGD/ESD mGy, lengths mm, times s, activity MBq, weight kg, height m.
* Patient groups (age/weight bands) are never stored; they are derived from
  ``study.age_days`` / ``study.weight_kg`` and a :class:`GroupScheme` at query time.
* No SQL reserved words as table/column names (MariaDB): e.g. ``procedure_name``.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import AsciiId, JSONType, Text


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


# --------------------------------------------------------------------------- enums


class AgePrecision(enum.StrEnum):
    exact = "exact"  # from birth date + study date
    dicom_age = "dicom_age"  # from PatientAge (e.g. 005Y) – granularity limited
    estimated = "estimated"
    unknown = "unknown"


class SizeSource(enum.StrEnum):
    measured = "measured"  # measured immediately before the examination
    chart_same_day = "chart_same_day"
    chart_72h = "chart_72h"
    estimated_reference = "estimated_reference"  # growth-reference median for age/sex
    estimated_visual = "estimated_visual"
    dicom_unknown = "dicom_unknown"  # present in DICOM, provenance unknown
    imported = "imported"  # bulk import (e.g. legacy system, CSV) – provenance unknown


class ClassificationStatus(enum.StrEnum):
    auto = "auto"  # derived by a mapping rule
    suggested = "suggested"  # proposed, awaiting human confirmation
    confirmed = "confirmed"


def _enum(e: type[enum.Enum], name: str):
    return Enum(e, name=name, native_enum=False, length=24, validate_strings=True)


# --------------------------------------------------------------------- organisation


class Institution(TimestampMixin, Base):
    __tablename__ = "institution"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(AsciiId(32), unique=True)
    name: Mapped[str] = mapped_column(Text(255))
    is_local: Mapped[bool] = mapped_column(Boolean, default=True)


class Device(TimestampMixin, Base):
    """A physical piece of equipment. Identity: observer UID > manufacturer+serial > tuple."""

    __tablename__ = "device"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institution.id"))
    manufacturer: Mapped[str | None] = mapped_column(Text(128))
    model_name: Mapped[str | None] = mapped_column(Text(128))
    serial_number: Mapped[str | None] = mapped_column(AsciiId(64))
    device_observer_uid: Mapped[str | None] = mapped_column(AsciiId(64))
    station_name: Mapped[str | None] = mapped_column(Text(64))
    institution_name: Mapped[str | None] = mapped_column(Text(255))
    department_name: Mapped[str | None] = mapped_column(Text(255))
    software_versions: Mapped[str | None] = mapped_column(Text(255))
    gantry_id: Mapped[str | None] = mapped_column(Text(64))
    identity_key: Mapped[str] = mapped_column(AsciiId(64), unique=True)  # sha256 of identity
    display_name: Mapped[str | None] = mapped_column(Text(128))
    modality_override: Mapped[str | None] = mapped_column(AsciiId(16))
    is_local: Mapped[bool] = mapped_column(Boolean, default=True)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("device.id"))
    first_study_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    last_study_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    n_studies: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    __table_args__ = (
        Index("ix_device_observer", "device_observer_uid"),
        Index("ix_device_serial", "manufacturer", "serial_number"),
    )


# -------------------------------------------------------------------------- patient


class Patient(TimestampMixin, Base):
    __tablename__ = "patient"
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[str | None] = mapped_column(Text(64), index=True)
    issuer: Mapped[str | None] = mapped_column(Text(64))
    name: Mapped[str | None] = mapped_column(Text(255))
    birth_date: Mapped[dt.date | None] = mapped_column(Date)
    sex: Mapped[str | None] = mapped_column(AsciiId(1))  # M / F / O
    # OpenREM-compatible sha256(utf8(id)) – links hashed legacy records to clear IDs
    legacy_sha256: Mapped[str | None] = mapped_column(AsciiId(64), index=True)
    # keyed pseudonym shown instead of identifiers (HMAC with the site secret, see security.py)
    pseudo_id: Mapped[str | None] = mapped_column(AsciiId(16))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    test_reason: Mapped[str | None] = mapped_column(Text(255))

    studies: Mapped[list[Study]] = relationship(back_populates="patient")

    __table_args__ = (UniqueConstraint("pseudo_id", name="uq_patient_pseudo_id"),)


# ---------------------------------------------------------------------------- study


class Study(TimestampMixin, Base):
    __tablename__ = "study"
    id: Mapped[int] = mapped_column(primary_key=True)
    study_uid: Mapped[str] = mapped_column(AsciiId(64), unique=True)
    accession: Mapped[str | None] = mapped_column(Text(64), index=True)
    accession_is_hashed: Mapped[bool] = mapped_column(Boolean, default=False)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patient.id"), index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id"), index=True)
    institution_id: Mapped[int | None] = mapped_column(ForeignKey("institution.id"))
    modality: Mapped[str] = mapped_column(AsciiId(16))
    study_datetime: Mapped[dt.datetime | None] = mapped_column(DateTime)
    study_description: Mapped[str | None] = mapped_column(Text(255))
    requested_procedure: Mapped[str | None] = mapped_column(Text(255))
    procedure_name: Mapped[str | None] = mapped_column(Text(255))
    performing_physician: Mapped[str | None] = mapped_column(Text(255))
    operator_name: Mapped[str | None] = mapped_column(Text(255))
    referring_physician: Mapped[str | None] = mapped_column(Text(255))

    # patient state at the time of the study
    age_days: Mapped[int | None] = mapped_column(Integer)
    age_precision: Mapped[AgePrecision] = mapped_column(
        _enum(AgePrecision, "age_precision"), default=AgePrecision.unknown
    )
    weight_kg: Mapped[float | None] = mapped_column(Float)
    weight_source: Mapped[SizeSource | None] = mapped_column(_enum(SizeSource, "size_source"))
    height_m: Mapped[float | None] = mapped_column(Float)
    height_source: Mapped[SizeSource | None] = mapped_column(_enum(SizeSource, "size_source"))
    pregnant: Mapped[bool | None] = mapped_column(Boolean)
    gestation_weeks: Mapped[int | None] = mapped_column(SmallInteger)
    contrast_mode: Mapped[str | None] = mapped_column(AsciiId(4))  # N / C / NC / MF
    drl_exclusion: Mapped[str | None] = mapped_column(Text(64))  # reason, NULL = eligible

    # precomputed totals (storage units) – maintained by ingest and `dosetrail recompute`
    n_events: Mapped[int] = mapped_column(Integer, default=0)
    n_spiral: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    n_axial: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    n_localizer: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ctdivol_max: Mapped[float | None] = mapped_column(Float)
    total_dlp: Mapped[float | None] = mapped_column(Float)
    total_dap: Mapped[float | None] = mapped_column(Float)
    total_rp_dose: Mapped[float | None] = mapped_column(Float)
    total_fluoro_time: Mapped[float | None] = mapped_column(Float)
    total_fluoro_dap: Mapped[float | None] = mapped_column(Float)
    total_acq_dap: Mapped[float | None] = mapped_column(Float)
    agd_left: Mapped[float | None] = mapped_column(Float)
    agd_right: Mapped[float | None] = mapped_column(Float)
    admin_activity_mbq: Mapped[float | None] = mapped_column(Float)

    source_kind: Mapped[str] = mapped_column(AsciiId(32))  # rdsr, dx_header, openrem, ...
    source_json: Mapped[dict | None] = mapped_column(JSONType)

    patient: Mapped[Patient | None] = relationship(back_populates="studies")
    device: Mapped[Device | None] = relationship()
    events: Mapped[list[Event]] = relationship(
        back_populates="study", cascade="all, delete-orphan", order_by="Event.event_datetime"
    )

    __table_args__ = (
        Index("ix_study_mod_dt", "modality", "study_datetime"),
        Index("ix_study_dt", "study_datetime"),
        Index("ix_study_device_dt", "device_id", "study_datetime"),
    )


class Event(Base):
    """One irradiation event (CT acquisition/series, projection exposure, fluoro run)."""

    __tablename__ = "event"
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"))
    event_uid: Mapped[str | None] = mapped_column(AsciiId(64))
    event_datetime: Mapped[dt.datetime | None] = mapped_column(DateTime)
    event_type: Mapped[str | None] = mapped_column(AsciiId(32))
    protocol: Mapped[str | None] = mapped_column(Text(255))
    comment: Mapped[str | None] = mapped_column(Text(255))
    plane: Mapped[str | None] = mapped_column(AsciiId(1))  # A / B
    anatomy: Mapped[str | None] = mapped_column(Text(128))
    laterality: Mapped[str | None] = mapped_column(AsciiId(2))
    view: Mapped[str | None] = mapped_column(Text(64))

    # CT
    ctdivol: Mapped[float | None] = mapped_column(Float)
    dlp: Mapped[float | None] = mapped_column(Float)
    ct_phantom_cm: Mapped[int | None] = mapped_column(SmallInteger)  # 16 / 32
    scan_length_mm: Mapped[float | None] = mapped_column(Float)
    pitch: Mapped[float | None] = mapped_column(Float)
    collimation_mm: Mapped[float | None] = mapped_column(Float)
    rotation_time_s: Mapped[float | None] = mapped_column(Float)
    tcm: Mapped[str | None] = mapped_column(Text(64))
    ssde: Mapped[float | None] = mapped_column(Float)
    wed_mm: Mapped[float | None] = mapped_column(Float)
    is_localizer: Mapped[bool] = mapped_column(Boolean, default=False)
    is_principal: Mapped[bool | None] = mapped_column(Boolean)
    dose_check_alert: Mapped[bool | None] = mapped_column(Boolean)

    # source parameters (all modalities)
    kvp: Mapped[float | None] = mapped_column(Float)
    ma: Mapped[float | None] = mapped_column(Float)
    ma_max: Mapped[float | None] = mapped_column(Float)
    mas: Mapped[float | None] = mapped_column(Float)
    exposure_time_s: Mapped[float | None] = mapped_column(Float)

    # projection radiography / fluoroscopy
    dap: Mapped[float | None] = mapped_column(Float)
    rp_dose: Mapped[float | None] = mapped_column(Float)
    pulse_rate: Mapped[float | None] = mapped_column(Float)
    pulse_width_ms: Mapped[float | None] = mapped_column(Float)
    n_pulses: Mapped[int | None] = mapped_column(Integer)
    fluoro_time_s: Mapped[float | None] = mapped_column(Float)
    filter_cu_mm: Mapped[float | None] = mapped_column(Float)
    filter_al_mm: Mapped[float | None] = mapped_column(Float)
    grid: Mapped[str | None] = mapped_column(Text(64))
    sid_mm: Mapped[float | None] = mapped_column(Float)
    ssd_mm: Mapped[float | None] = mapped_column(Float)
    field_w_mm: Mapped[float | None] = mapped_column(Float)
    field_h_mm: Mapped[float | None] = mapped_column(Float)
    primary_angle: Mapped[float | None] = mapped_column(Float)
    secondary_angle: Mapped[float | None] = mapped_column(Float)

    # detector
    exposure_index: Mapped[float | None] = mapped_column(Float)
    target_exposure_index: Mapped[float | None] = mapped_column(Float)
    deviation_index: Mapped[float | None] = mapped_column(Float)
    ei_vendor_value: Mapped[float | None] = mapped_column(Float)
    ei_vendor_unit: Mapped[str | None] = mapped_column(Text(32))

    # mammography
    agd: Mapped[float | None] = mapped_column(Float)
    esd: Mapped[float | None] = mapped_column(Float)
    compression_thickness_mm: Mapped[float | None] = mapped_column(Float)
    compression_force_n: Mapped[float | None] = mapped_column(Float)
    target_material: Mapped[str | None] = mapped_column(Text(32))
    filter_material: Mapped[str | None] = mapped_column(Text(32))
    exposure_control_mode: Mapped[str | None] = mapped_column(Text(64))

    extra_json: Mapped[dict | None] = mapped_column(JSONType)

    study: Mapped[Study] = relationship(back_populates="events")

    __table_args__ = (UniqueConstraint("study_id", "event_uid", name="uq_event_study_uid"),)


class IngestedObject(Base):
    """Every DICOM object (or legacy record) seen – drives idempotency."""

    __tablename__ = "ingested_object"
    id: Mapped[int] = mapped_column(primary_key=True)
    sop_instance_uid: Mapped[str] = mapped_column(AsciiId(64), unique=True)
    sop_class_uid: Mapped[str | None] = mapped_column(AsciiId(64))
    study_id: Mapped[int | None] = mapped_column(ForeignKey("study.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(AsciiId(32))
    status: Mapped[str] = mapped_column(AsciiId(16))  # imported / skipped / failed
    message: Mapped[str | None] = mapped_column(Text(1024))
    received_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())


# ------------------------------------------------------------------ classification


class CodeSystem(TimestampMixin, Base):
    __tablename__ = "code_system"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(AsciiId(64), unique=True)
    name: Mapped[str] = mapped_column(Text(255))
    version: Mapped[str | None] = mapped_column(Text(32))
    source: Mapped[str | None] = mapped_column(Text(512))
    author: Mapped[str | None] = mapped_column(Text(255))
    licence: Mapped[str | None] = mapped_column(Text(128))

    codes: Mapped[list[Code]] = relationship(back_populates="system")


class Code(Base):
    __tablename__ = "code"
    id: Mapped[int] = mapped_column(primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("code_system.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(AsciiId(64))
    meaning: Mapped[str] = mapped_column(Text(512))
    chapter: Mapped[str | None] = mapped_column(Text(128))
    modality: Mapped[str | None] = mapped_column(AsciiId(16))
    attributes: Mapped[dict | None] = mapped_column(JSONType)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    system: Mapped[CodeSystem] = relationship(back_populates="codes")

    __table_args__ = (UniqueConstraint("system_id", "code", name="uq_code_system_code"),)


class CodeMapping(TimestampMixin, Base):
    """Rule: a local text value (description/protocol) maps to a code in some system."""

    __tablename__ = "code_mapping"
    id: Mapped[int] = mapped_column(primary_key=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("code.id", ondelete="CASCADE"))
    match_field: Mapped[str] = mapped_column(AsciiId(32))  # study_description, protocol, ...
    match_value: Mapped[str] = mapped_column(Text(255))  # normalised (casefold, trimmed)
    modality: Mapped[str | None] = mapped_column(AsciiId(16))
    status: Mapped[ClassificationStatus] = mapped_column(
        _enum(ClassificationStatus, "classification_status"),
        default=ClassificationStatus.suggested,
    )

    __table_args__ = (Index("ix_mapping_lookup", "match_field", "match_value"),)


class Classification(Base):
    """Links a study (or one event of it) to a code. Many per study, in many systems."""

    __tablename__ = "classification"
    id: Mapped[int] = mapped_column(primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("study.id", ondelete="CASCADE"))
    event_id: Mapped[int | None] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    code_id: Mapped[int] = mapped_column(ForeignKey("code.id", ondelete="CASCADE"))
    status: Mapped[ClassificationStatus] = mapped_column(
        _enum(ClassificationStatus, "classification_status"), default=ClassificationStatus.auto
    )
    mapping_id: Mapped[int | None] = mapped_column(
        ForeignKey("code_mapping.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_classification_study", "study_id"),
        Index("ix_classification_code", "code_id"),
        Index("ix_classification_event", "event_id"),
    )


# ---------------------------------------------------------------- groups & DRLs


class GroupScheme(Base):
    """A set of half-open bands on age or weight (e.g. RP 185 head-age groups)."""

    __tablename__ = "group_scheme"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(AsciiId(64), unique=True)
    name: Mapped[str] = mapped_column(Text(255))
    parameter: Mapped[str] = mapped_column(AsciiId(8))  # age / weight
    source: Mapped[str | None] = mapped_column(Text(512))

    bands: Mapped[list[GroupBand]] = relationship(
        back_populates="scheme", order_by="GroupBand.sort_order", cascade="all, delete-orphan"
    )


class GroupBand(Base):
    """Band [lower, upper). Units: age in 'd'/'mo'/'y' (calendar arithmetic), weight 'kg'."""

    __tablename__ = "group_band"
    id: Mapped[int] = mapped_column(primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("group_scheme.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(AsciiId(32))
    label: Mapped[str] = mapped_column(Text(64))
    lower: Mapped[float | None] = mapped_column(Float)
    lower_unit: Mapped[str | None] = mapped_column(AsciiId(4))
    upper: Mapped[float | None] = mapped_column(Float)
    upper_unit: Mapped[str | None] = mapped_column(AsciiId(4))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    scheme: Mapped[GroupScheme] = relationship(back_populates="bands")

    __table_args__ = (UniqueConstraint("scheme_id", "key", name="uq_band_scheme_key"),)


class ReferenceSet(TimestampMixin, Base):
    __tablename__ = "reference_set"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(AsciiId(64), unique=True)
    name: Mapped[str] = mapped_column(Text(255))
    source: Mapped[str | None] = mapped_column(Text(512))
    valid_from: Mapped[dt.date | None] = mapped_column(Date)
    valid_to: Mapped[dt.date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text(1024))

    values: Mapped[list[ReferenceValue]] = relationship(
        back_populates="reference_set", cascade="all, delete-orphan"
    )


class ReferenceValue(Base):
    __tablename__ = "reference_value"
    id: Mapped[int] = mapped_column(primary_key=True)
    set_id: Mapped[int] = mapped_column(ForeignKey("reference_set.id", ondelete="CASCADE"))
    exam_code_id: Mapped[int] = mapped_column(ForeignKey("code.id"))
    band_id: Mapped[int | None] = mapped_column(ForeignKey("group_band.id"))  # NULL = all ages
    quantity: Mapped[str] = mapped_column(AsciiId(16))  # dlp ctdivol dap kae mgd activity
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(AsciiId(16))  # UCUM: mGy.cm mGy Gy.cm2 mGy.cm2 MBq
    applies_to: Mapped[str] = mapped_column(AsciiId(16))  # series / study / exposure

    reference_set: Mapped[ReferenceSet] = relationship(back_populates="values")
    exam_code: Mapped[Code] = relationship()
    band: Mapped[GroupBand | None] = relationship()


class UnitConversion(Base):
    """Admin-editable conversions to storage units (e.g. cGy.cm2 -> Gy.cm2 factor 0.01)."""

    __tablename__ = "unit_conversion"
    id: Mapped[int] = mapped_column(primary_key=True)
    quantity: Mapped[str] = mapped_column(AsciiId(16))
    from_unit: Mapped[str] = mapped_column(AsciiId(32))
    to_unit: Mapped[str] = mapped_column(AsciiId(32))
    factor: Mapped[float] = mapped_column(Float)

    __table_args__ = (UniqueConstraint("quantity", "from_unit", name="uq_unit_conv"),)


# ------------------------------------------------------------------- users & audit


class User(TimestampMixin, Base):
    __tablename__ = "app_user"  # "user" is reserved in several SQL dialects
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(AsciiId(64), unique=True)
    display_name: Mapped[str | None] = mapped_column(Text(128))
    password_hash: Mapped[str] = mapped_column(AsciiId(255))
    roles: Mapped[str] = mapped_column(AsciiId(255), default="viewer")  # comma-separated
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    def role_set(self) -> set[str]:
        return {r.strip() for r in (self.roles or "").split(",") if r.strip()}


class AuditLog(Base):
    """Append-only record of sensitive actions (identifier reveal, exports, admin changes)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("app_user.id", ondelete="SET NULL"))
    username: Mapped[str | None] = mapped_column(AsciiId(64))
    action: Mapped[str] = mapped_column(AsciiId(64))
    target: Mapped[str | None] = mapped_column(Text(255))
    detail: Mapped[dict | None] = mapped_column(JSONType)
    remote_addr: Mapped[str | None] = mapped_column(AsciiId(64))


class AppSetting(Base):
    """Small key/value store for administrative state (e.g. dismissed merge suggestions)."""

    __tablename__ = "app_setting"
    key: Mapped[str] = mapped_column(AsciiId(128), primary_key=True)
    value: Mapped[dict | list | None] = mapped_column(JSONType)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

