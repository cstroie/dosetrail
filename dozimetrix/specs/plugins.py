"""Trusted plugins: Python files that register fields, transforms, filters, kinds or writers.

Every ``*.py`` in ``[specs] plugins_dir`` is imported once, at start-up. A plugin imports
``dozimetrix.specs.registry`` and decorates functions; that is all it needs to do. **A plugin runs
with the server's rights**, so the folder is for the administrator only. A plugin that fails to
import is skipped and reported; the others and the program keep working. The name and SHA-256 of
every file loaded are written to the audit log, so a change to a plugin is visible.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def load(folder: Path | None) -> list[dict]:
    out: list[dict] = []
    if not folder or not Path(folder).is_dir():
        return out
    for path in sorted(Path(folder).glob("*.py")):
        entry = {"file": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        try:
            spec = importlib.util.spec_from_file_location(f"dozimetrix_plugin_{path.stem}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            entry["error"] = None
        except Exception as e:  # a bad plugin must not stop the program
            entry["error"] = f"{type(e).__name__}: {e}"
            log.warning("plugin %s failed: %s", path, e)
        out.append(entry)
    return out
