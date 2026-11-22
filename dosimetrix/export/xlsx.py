"""A small, streaming XLSX writer (Office Open XML SpreadsheetML), standard library only.

Enough for data exports:
* several sheets;
* numbers, text (inline strings), booleans, dates and datetimes (real Excel dates with a
  display format);
* a bold, frozen header row, an autofilter and column widths.

Rows are written straight into the zip entry, so memory stays flat for a million rows.
Excel's limit of 1,048,576 rows per sheet is enforced.

    with XlsxWriter(path_or_file) as wb:
        ws = wb.sheet("Studies", ["Date", "DLP"], widths=[12, 10])
        ws.row([dt.date(2026, 1, 2), 812.5])
"""

from __future__ import annotations

import datetime as dt
import math
import zipfile
from typing import IO
from xml.sax.saxutils import escape

MAX_ROWS = 1_048_576
_EPOCH = dt.datetime(1899, 12, 30)
_ILLEGAL = {c: None for c in range(32) if c not in (9, 10, 13)}

# style ids in styles.xml: 0 default, 1 bold header, 2 date, 3 datetime
_STYLES = "".join([
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
    '<numFmts count="2"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>',
    '<numFmt numFmtId="165" formatCode="yyyy\\-mm\\-dd hh:mm:ss"/></numFmts>',
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>',
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>',
    '<fills count="2"><fill><patternFill patternType="none"/></fill>',
    '<fill><patternFill patternType="gray125"/></fill></fills>',
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>',
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>',
    '<cellXfs count="4">',
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>',
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>',
    '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>',
    '<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>',
    '</cellXfs>',
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>',
    '</styleSheet>',
])
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_PKG = "http://schemas.openxmlformats.org/package/2006"
_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SML = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml"
_RELS_CT = "application/vnd.openxmlformats-package.relationships+xml"


def col_name(i: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(v: str) -> str:
    return escape(str(v).translate(_ILLEGAL))


def _cell(ref: str, v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return f'<c r="{ref}" t="b"><v>{int(v)}</v></c>'
    if isinstance(v, (int, float)):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return ""
        return f'<c r="{ref}"><v>{v!r}</v></c>' if isinstance(v, float) else \
            f'<c r="{ref}"><v>{v}</v></c>'
    if isinstance(v, dt.datetime):
        serial = (v.replace(tzinfo=None) - _EPOCH).total_seconds() / 86400
        return f'<c r="{ref}" s="3"><v>{serial:.8f}</v></c>'
    if isinstance(v, dt.date):
        serial = (dt.datetime(v.year, v.month, v.day) - _EPOCH).days
        return f'<c r="{ref}" s="2"><v>{serial}</v></c>'
    space = ' xml:space="preserve"' if str(v) != str(v).strip() else ""
    return f'<c r="{ref}" t="inlineStr"><is><t{space}>{_text(v)}</t></is></c>'


class Sheet:
    def __init__(self, book: XlsxWriter, index: int, name: str, header: list[str],
                 widths: list[float] | None):
        self.book, self.index, self.name, self.ncols = book, index, name, len(header)
        self.rows = 0
        self.fh = book.zip.open(f"xl/worksheets/sheet{index}.xml", "w", force_zip64=True)
        cols = ""
        if widths:
            cols = "<cols>" + "".join(
                f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                for i, w in enumerate(widths)) + "</cols>"
        self._w(f'{_XML}<worksheet xmlns="{_SML}">'
                '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
                'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                f'{cols}<sheetData>')
        cells = "".join(f'<c r="{col_name(i)}1" t="inlineStr" s="1"><is><t>{_text(h)}</t></is></c>'
                        for i, h in enumerate(header))
        self._w(f'<row r="1">{cells}</row>')
        self.rows = 1

    def _w(self, s: str) -> None:
        self.fh.write(s.encode("utf-8"))

    def row(self, values) -> None:
        if self.rows >= MAX_ROWS:
            raise OverflowError(f"sheet {self.name!r} is full ({MAX_ROWS} rows)")
        self.rows += 1
        r = self.rows
        self._w(f'<row r="{r}">' + "".join(_cell(f"{col_name(i)}{r}", v)
                                           for i, v in enumerate(values)) + "</row>")

    def close(self) -> None:
        last = f"{col_name(max(self.ncols - 1, 0))}{self.rows}"
        self._w(f'</sheetData><autoFilter ref="A1:{last}"/></worksheet>')
        self.fh.close()


class XlsxWriter:
    def __init__(self, target: str | IO[bytes]):
        self.zip = zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED)
        self.sheets: list[Sheet] = []
        self._open: Sheet | None = None

    def sheet(self, name: str, header: list[str], widths: list[float] | None = None) -> Sheet:
        if self._open is not None:
            self._open.close()
        clean = "".join(c for c in name if c not in '[]:*?/\\')[:31]
        clean = clean or f"Sheet{len(self.sheets) + 1}"
        s = Sheet(self, len(self.sheets) + 1, clean, header, widths)
        self.sheets.append(s)
        self._open = s
        return s

    def close(self) -> None:
        if self._open is not None:
            self._open.close()
            self._open = None
        n = len(self.sheets)
        z = self.zip
        overrides = [f'<Override PartName="/xl/workbook.xml" ContentType="{_CT}.sheet.main+xml"/>',
                     f'<Override PartName="/xl/styles.xml" ContentType="{_CT}.styles+xml"/>']
        overrides += [f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                      f'ContentType="{_CT}.worksheet+xml"/>' for i in range(1, n + 1)]
        z.writestr("[Content_Types].xml", "".join([
            _XML, f'<Types xmlns="{_PKG}/content-types">',
            f'<Default Extension="rels" ContentType="{_RELS_CT}"/>',
            '<Default Extension="xml" ContentType="application/xml"/>', *overrides, "</Types>"]))
        z.writestr("_rels/.rels", "".join([
            _XML, f'<Relationships xmlns="{_PKG}/relationships">',
            f'<Relationship Id="rId1" Type="{_DOC}/officeDocument" Target="xl/workbook.xml"/>',
            "</Relationships>"]))
        sheets = "".join(f'<sheet name="{_text(s.name)}" sheetId="{s.index}" r:id="rId{s.index}"/>'
                         for s in self.sheets)
        z.writestr("xl/workbook.xml", "".join([
            _XML, f'<workbook xmlns="{_SML}" xmlns:r="{_DOC}">',
            f"<sheets>{sheets}</sheets></workbook>"]))
        rels = [f'<Relationship Id="rId{i}" Type="{_DOC}/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1)]
        rels.append(f'<Relationship Id="rId{n + 1}" Type="{_DOC}/styles" Target="styles.xml"/>')
        z.writestr("xl/_rels/workbook.xml.rels", "".join([
            _XML, f'<Relationships xmlns="{_PKG}/relationships">', *rels, "</Relationships>"]))
        z.writestr("xl/styles.xml", _STYLES)
        z.close()

    def __enter__(self) -> XlsxWriter:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
