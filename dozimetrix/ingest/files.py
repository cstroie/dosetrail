"""Import DICOM files and folders (each object in its own savepoint; commits in batches)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import pydicom
from pydicom import config as dicom_config
from pydicom.errors import InvalidDicomError
from sqlalchemy.orm import Session

from .. import centering
from . import dosepage
from .parse import parse
from .store import Result, Store

# Vendors write out-of-spec values (e.g. 16-character IS); read them, don't warn per file.
dicom_config.settings.reading_validation_mode = dicom_config.IGNORE


def iter_files(paths: Iterable[str | Path]) -> Iterator[Path]:
    for p in map(Path, paths):
        if p.is_dir():
            yield from sorted(x for x in p.rglob("*") if x.is_file())
        elif p.is_file():
            yield p


def read(path: Path):
    """The object's header; the pixels too for the few objects whose picture is read (a
    scanner's dose screenshot, see dosepage.py)."""
    try:
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        force = False
    except InvalidDicomError:
        ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
        force = True
    if dosepage.wants_pixels(ds) or centering.wants_pixels(ds):
        ds = pydicom.dcmread(path, force=force)
    return ds


def import_paths(session: Session, paths: Iterable[str | Path], secret: bytes,
                 batch: int = 200, progress: Callable[[int, Path, Result], None] | None = None,
                 ) -> Store:
    store = Store(session, secret, source="file")
    for i, path in enumerate(iter_files(paths), 1):
        try:
            ds = read(path)
            if "SOPInstanceUID" not in ds:
                store.stats["not_dicom"] += 1
                continue
            with session.begin_nested():
                result = store.store(parse(ds))
        except Exception as e:  # one bad file must not stop the batch
            store.stats["failed"] += 1
            result = Result("failed", message=f"{type(e).__name__}: {e}"[:300])
        if progress:
            progress(i, path, result)
        if i % batch == 0:
            session.commit()
    session.commit()
    return store
