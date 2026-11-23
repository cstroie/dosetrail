"""The scanner's own dose report, read from a screenshot (Secondary Capture) with tesseract.

Some CT scanners send no radiation dose SR, only a picture of the console's dose page. The
Toshiba Aquilion's is a dark 1024x1024 RGB page set in a series of its own (series 9000):
page 1 names the patient (ID, weight) and gives the total DLP, page 2 (and the following
ones) lists every acquisition with its CTDIvol and DLP. Reading it gives the exact values
the technologist sees, where the image headers only allow an estimate.

The image is never stored: only the numbers leave this module. Needs the optional ``ocr``
extra (Pillow, numpy) and the ``tesseract`` binary; without them ``read`` returns None and
the study keeps its header estimate.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

SECONDARY_CAPTURE = "1.2.840.10008.5.1.4.1.1.7"
THRESHOLD = 120  # the page is dark: text (>= ~190) is kept, grid lines and row shading (< ~100) go
_warned = False

_PID = re.compile(r"Patient\s*ID\s*:\s*([0-9A-Za-z]+)")
_DATE = re.compile(r"Study\s*Date\s*:\s*(\d{4})/(\d{2})/(\d{2})")
_INFO = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*/\s*(\d+)\s*/\s*\w+\s*/\s*(\d+(?:\.\d+)?)\s*\(kg\)")
_TOTAL = re.compile(r"Total\s*DLP.*?\(Head\)\s*:\s*(\S+)\s*\(Body\)\s*:\s*(\S+)", re.S)
_DECIMALS = re.compile(r"\d+\.\d{2}")
_ROW = re.compile(r"^(\d{1,2})\s+(\S.*?)\s+(\d{1,2})\s+(\d{2,3})\b(.*)$")
_SECTION = re.compile(r"^(\d{1,2})\s*\.\s*(\S.*)$")


def wants_pixels(ds) -> bool:
    """A Toshiba Secondary Capture: the candidate dose page, whose picture is read."""
    return str(ds.get("SOPClassUID", "")) == SECONDARY_CAPTURE \
        and "TOSHIBA" in str(ds.get("Manufacturer", "")).upper()


def available() -> bool:
    return shutil.which("tesseract") is not None


def render(ds) -> bytes | None:
    """The page as black text on white, twice the size, ready for tesseract."""
    global _warned
    try:
        import numpy as np
        from PIL import Image
        a = ds.pixel_array
    except Exception as e:  # noqa: BLE001 – no extra, no pixel decoder, unreadable pixels
        if not _warned:
            log.warning("dose pages are not read (%s): pip install 'dosimetrix[ocr]'", e)
            _warned = True
        return None
    if a.ndim == 3:
        a = a.max(axis=2)
    page = Image.fromarray(np.where(a > THRESHOLD, 0, 255).astype("uint8"))
    page = page.resize((page.width * 2, page.height * 2), Image.LANCZOS)
    out = io.BytesIO()
    page.save(out, "PNG")
    return out.getvalue()


def ocr(png: bytes) -> str | None:
    try:
        r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"],
                           input=png, capture_output=True, timeout=60, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("tesseract failed: %s", e)
        return None
    return r.stdout.decode("utf-8", "replace")


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def parse_text(text: str) -> dict | None:
    """What the OCR text of one page says: patient_id, study_date (YYYYMMDD), weight_kg,
    total_dlp (page 1) and rows (page 2...): {section, no, protocol, kvp, ctdivol, dlp, phantom}.
    None when the text is neither a summary nor a table page."""
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    flat = "\n".join(lines)
    out: dict = {"rows": []}
    if m := _PID.search(flat):
        out["patient_id"] = m.group(1)
    if m := _DATE.search(flat):
        out["study_date"] = "".join(m.groups())
    if m := _INFO.search(flat):
        out["weight_kg"] = _num(m.group(5))
    if m := _TOTAL.search(flat):
        vals = [v for v in map(_num, m.groups()) if v is not None]
        out["total_dlp"] = sum(vals) if vals else None
    section, pending, started = None, None, False
    for ln in lines:
        if "CTDIvol" in ln or "DLP" in ln and "Total" not in ln:
            started = True
        if "Exposure" in ln:  # the SUREExposure table that follows: not dose
            started = False
        if not started:
            if (s := _SECTION.match(ln)) and not _DECIMALS.search(ln):
                section = (int(s.group(1)), s.group(2).strip())
            continue
        row = _ROW.match(ln)
        if row and section is not None and not (_DECIMALS.search(ln) and not row.group(5).strip()):
            no, protocol, _n, kvp, rest = row.groups()
            if pending and pending[0].upper().startswith("SURE"):
                protocol = "SURE" + protocol
            ctdi, dlp = (pending[1], pending[2]) if pending else (None, None)
            out["rows"].append({
                "section": section[0], "exam": section[1], "no": int(no), "protocol": protocol,
                "kvp": float(kvp), "ctdivol": ctdi, "dlp": dlp,
                "phantom": 16 if "(Head)" in rest else 32 if "(Body)" in rest else None})
            pending = None
            continue
        dec = _DECIMALS.findall(ln)
        pending = (ln, float(dec[-2]), float(dec[-1])) if len(dec) >= 2 else None
    if not out["rows"] and out.get("total_dlp") is None and not out.get("weight_kg"):
        return None
    return out


def event_type(protocol: str) -> str:
    p = protocol.lower()
    if re.search(r"scano|scout|surview|topo|locali", p):
        return "localizer"
    if "helic" in p or "spiral" in p:
        return "spiral"
    return "axial"  # S&V, SUREStart: stationary monitoring / preview scans


def event_uid(study_uid: str, section: int, no: int) -> str:
    return "dp-" + hashlib.sha1(f"{study_uid}|{section}|{no}".encode()).hexdigest()[:40]


def read(ds) -> dict | None:
    """OCR one Secondary Capture object; None when it is not a readable dose page."""
    if not available():
        return None
    png = render(ds)
    text = ocr(png) if png else None
    return parse_text(text) if text else None
