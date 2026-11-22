"""Developer tool: regenerate bundled seed CSVs from the reference spreadsheets.

The spreadsheets live in the git-ignored `reference/` directory; only the extracted,
versioned CSVs under dosimetrix/seeds/ are committed. Requires openpyxl + xlrd, which
are NOT Dosimetrix dependencies – run with any interpreter that has them:

    python tools/extract_seeds.py reference/ dosimetrix/seeds/codes/
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path


def write(path: Path, rows: list[dict]) -> None:
    cols = ["code", "meaning", "chapter", "modality", "attributes"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["attributes"] = json.dumps(r.get("attributes") or {}, ensure_ascii=False) \
                if r.get("attributes") else ""
            w.writerow({c: r.get(c, "") or "" for c in cols})
    print(f"{path.name}: {len(rows)} codes")


def clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def nomenclator(xlsx: Path, sheets: dict[str, str], modality_by_prefix: dict[str, str]):
    import openpyxl

    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        if ws.title not in sheets:
            continue
        chapter = ""
        header_seen = False
        for row in ws.iter_rows(values_only=True):
            cells = [clean(c) for c in row]
            nonempty = [c for c in cells if c]
            if not nonempty:
                continue
            if not header_seen:
                header_seen = "Cod" in cells
                continue
            code = cells[1] if len(cells) > 1 else ""
            meaning = cells[2] if len(cells) > 2 else ""
            if code.startswith("Total"):
                continue
            if code and not meaning:
                chapter = code
                continue
            if not code:
                continue
            attrs = {}
            if len(cells) > 3 and cells[3]:
                attrs["bilateral"] = cells[3]
            prefix = code.split("-", 1)[0]
            out.append({
                "code": code, "meaning": meaning, "chapter": chapter or sheets[ws.title],
                "modality": modality_by_prefix.get(prefix, ""), "attributes": attrs,
            })
    return out


def insp_quarterly(xls: Path):
    """National procedure codes from the INSP quarterly centralised report (Order 1255/2016)."""
    import xlrd

    wb = xlrd.open_workbook(str(xls))
    wanted = {  # sheet -> (code-system suffix, modality); codes are unique only per sheet
        "Radiologie": ("rx", "RX"), "CT": ("ct", "CT"), "Interventionala RX": ("xa", "XA"),
        "Interventionala CT": ("ct-int", "CT"), "Dentare": ("dental", "DX"),
        "MN diagnostic": ("nm", "NM"), "MN Hibride": ("nm-hybrid", "NM"),
    }
    systems: dict[str, list[dict]] = {}
    for ws in wb.sheets():
        if ws.name not in wanted:
            continue
        # locate header row and code column
        hdr_r = code_c = None
        for r in range(min(ws.nrows, 10)):
            for c, v in enumerate(ws.row_values(r)):
                if clean(v).startswith("Cod"):
                    hdr_r, code_c = r, c
                    break
            if hdr_r is not None:
                break
        if hdr_r is None:
            continue
        exam_c = next(c for c, v in enumerate(ws.row_values(hdr_r)) if clean(v))
        suffix, modality = wanted[ws.name]
        out, seen = systems.setdefault(suffix, []), set()
        exam = section = ""
        for r in range(hdr_r + 1, ws.nrows):
            vals = [clean(v) for v in ws.row_values(r)]
            raw_code = vals[code_c]
            code = raw_code.rstrip("*").strip()
            first = vals[exam_c]
            if first.upper().startswith("TOTAL"):
                continue  # sections have subtotals; later sections follow
            if first.startswith(("*", "Se vor", "INAPOI")) or raw_code.startswith("INAPOI"):
                break  # footnotes after the last table
            if first and not code and first.isupper():
                section = first
                continue
            if first:
                exam = first
            if not code or code in seen or code.startswith(("M", "F")) and len(code) == 1:
                continue
            extra = [v for v in vals[exam_c + 1:code_c] if v]
            meaning = exam + (f" — {' / '.join(extra)}" if extra else "")
            attrs = {}
            if suffix == "ct":
                attrs["contrast"] = code.endswith("SC")
                if code.endswith("SC"):
                    meaning += " (contrast)"
            if raw_code != code:
                attrs["footnote"] = raw_code[len(code):]
            seen.add(code)
            out.append({"code": code, "meaning": meaning, "chapter": section or ws.name,
                        "modality": modality, "attributes": attrs})
    return systems


def main(ref: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    ct = ref / "Nomenclator_CT_v1.2.xlsx"
    write(dest / "ro-nom-ct.csv", nomenclator(
        ct, {"CT": "CT", "Traumă": "Traumă", "Angio-CT": "Angio-CT",
             "Proceduri ghidate": "Proceduri ghidate"}, {"CT": "CT", "AG": "CT", "PG": "CT"}))
    write(dest / "ro-nom-ct-contrast.csv",
          nomenclator(ct, {"Nativ - Contrast": "Protocol"}, {}))
    rx = ref / "Nomenclator_Radiologie_Conventionala_v1.3.xlsx"
    write(dest / "ro-nom-rx.csv", nomenclator(
        rx, {"RG — Radiografie": "Radiografie", "RF — Fluoroscopie": "Fluoroscopie"},
        {"RG": "DX", "RF": "RF"}))
    write(dest / "ro-nom-rx-projection.csv", nomenclator(rx, {"Incidențe": "Incidență"}, {}))
    ind = ref / "Nomenclator_Indicatii_v1.0.xlsx"
    import openpyxl
    sheets = {ws.title: ws.title for ws in openpyxl.load_workbook(ind, read_only=True).worksheets
              if not ws.title.startswith("Doc")}
    write(dest / "ro-nom-indications.csv", nomenclator(ind, sheets, {}))
    for suffix, rows in insp_quarterly(
            ref / "insp-forms" / "Formular_centralizat_unitati.xls").items():
        write(dest / f"ro-insp-1255-{suffix}.csv", rows)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
