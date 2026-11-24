#!/usr/bin/env python3
"""Repair the ages of studies whose patient has an impossible birth date.

Older imports took a placeholder birth date (1753-01-01, "unknown" in SQL Server) for a real
one, so the study got an "exact" age of 120-270 years. This tool

1. clears every patient birth date before 1901 (a valid national ID has already replaced the
   ones it could: `dozimetrix cnp backfill --apply`);
2. gives each study older than 120 years the age the legacy OpenREM dump recorded (DICOM
   Patient Age, else the decimal age), when that age is plausible (0-100 years);
3. sets the age of every other such study to unknown;
4. re-estimates the weights and exclusions of the changed studies.

Dry run by default; --apply writes (take a database backup first). Usage:

    python tools/repair_placeholder_ages.py /path/to/openrem-dump [--apply]
"""

from __future__ import annotations

import argparse
import csv
import getpass
import gzip
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import select

from dozimetrix import growth
from dozimetrix.config import load_settings
from dozimetrix.db.models import AgePrecision, AuditLog, Patient, Study
from dozimetrix.db.session import make_engine, make_sessionmaker
from dozimetrix.patient_age import parse_dicom_age

MAX_DAYS = 120 * 365      # the studies to repair: older than this
PLAUSIBLE_DAYS = 100 * 365  # a recovered age above this is not believed
UNITS = {"D": 1, "W": 7, "M": 30.4375, "Y": 365.25}


def legacy_age(row: dict) -> tuple[int | None, str | None]:
    """(age in days, which field) from an OpenREM study row, None when absent or impossible."""
    parsed = parse_dicom_age(row.get("patient_age"))
    if parsed:
        amount, unit = parsed
        days = int(amount * UNITS[unit])
        return (days, "patient_age") if 0 <= days <= PLAUSIBLE_DAYS else (None, None)
    try:
        dec = float(row["patient_age_decimal"]) if row.get("patient_age_decimal") else None
    except ValueError:
        dec = None
    if dec is not None and 0 < dec * 365.25 <= PLAUSIBLE_DAYS:
        return int(dec * 365.25), "age_decimal"
    return None, None


def load_dump(path: Path) -> dict[str, dict]:
    csv.field_size_limit(sys.maxsize)
    out = {}
    with gzip.open(path / "studies.csv.gz", "rt", newline="") as fh:
        for r in csv.DictReader(fh):
            out[r["study_instance_uid"][:64]] = {
                "patient_age": r.get("patient_age"),
                "patient_age_decimal": r.get("patient_age_decimal")}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dump", type=Path, help="the OpenREM dump directory (studies.csv.gz)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = ap.parse_args()

    legacy = load_dump(args.dump)
    maker = make_sessionmaker(make_engine(load_settings().database_url))
    counts: Counter = Counter()
    with maker() as session:
        bad = session.scalars(select(Patient).where(Patient.birth_date < "1901-01-01")).all()
        for p in bad:
            counts["birth_dates_cleared"] += 1
            p.birth_date = None
        studies = session.scalars(select(Study).where(Study.age_days > MAX_DAYS)).all()
        changed = []
        for st in studies:
            days, how = legacy_age(legacy[st.study_uid]) if st.study_uid in legacy else (None, None)
            if days is not None:
                st.age_days, st.age_precision = days, AgePrecision.dicom_age
                counts[f"age_from_{how}"] += 1
            else:
                st.age_days, st.age_precision = None, AgePrecision.unknown
                counts["age_unknown"] += 1
            changed.append(st.id)
        session.flush()
        if args.apply:
            for i in range(0, len(changed), 5000):
                chunk = changed[i:i + 5000]
                growth.estimate_weights(session, chunk)
                growth.apply_exclusions(session, chunk)
            session.add(AuditLog(username=f"cli:{getpass.getuser()}", action="age_repair",
                                 target="studies", detail=dict(counts)))
            session.commit()
        else:
            session.rollback()
    print(("applied" if args.apply else "dry run (--apply writes)"), dict(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
