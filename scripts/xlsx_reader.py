"""Minimal .xlsx reader — stdlib only (zipfile + xml.etree).

An .xlsx is a zip of XML parts. This reads just enough of that format to get
sheet names and row data out, keyed by header text, with 1-based row numbers
preserved for provenance (SPEC §4: source_row).

Deliberately not a general-purpose xlsx library — no styles, no merged
cells, no formula evaluation beyond the cached <v> value. Good enough for
tabular ledger data with a header row.
"""
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def excel_serial_to_date(serial):
    """Excel's date epoch is 1899-12-30 (it carries a deliberate off-by-one
    to reproduce Lotus 1-2-3's leap-year bug). Returns None if not numeric."""
    try:
        n = float(serial)
    except (TypeError, ValueError):
        return None
    return date(1899, 12, 30) + timedelta(days=n)


def _col_letters(ref):
    return "".join(ch for ch in ref if ch.isalpha())


def _col_to_idx(col_letters):
    idx = 0
    for c in col_letters:
        idx = idx * 26 + (ord(c) - ord("A") + 1)
    return idx - 1


def _load_shared_strings(z):
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return [
        "".join(t.text or "" for t in si.findall(".//m:t", NS))
        for si in root.findall("m:si", NS)
    ]


def _load_sheet_targets(z):
    """Sheet name -> worksheet XML path, in workbook order."""
    wb_root = ET.fromstring(z.read("xl/workbook.xml"))
    rels_root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {rel.get("Id"): rel.get("Target") for rel in rels_root}
    result = {}
    for sheet in wb_root.findall(".//m:sheets/m:sheet", NS):
        rid = sheet.get(f"{REL_NS}id")
        target = rid_to_target.get(rid)
        if target and not target.startswith("/"):
            target = "xl/" + target
        result[sheet.get("name")] = target
    return result


def _cell_value(c, shared_strings):
    ctype = c.get("t")
    v = c.find("m:v", NS)
    is_ = c.find("m:is", NS)
    if is_ is not None:
        t = is_.find(".//m:t", NS)
        return t.text if t is not None else None
    if v is None:
        return None
    raw = v.text
    if ctype == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    return raw


class Workbook:
    """Open once, read multiple sheets. Use as a context manager."""

    def __init__(self, path):
        self.path = path
        self._zip = zipfile.ZipFile(path)
        self._shared_strings = _load_shared_strings(self._zip)
        self._sheet_targets = _load_sheet_targets(self._zip)

    def sheet_names(self):
        return list(self._sheet_targets.keys())

    def read_sheet(self, name):
        """Returns list of (row_number, {header: value}) for every row below
        the header, skipping rows with no header-mapped values at all."""
        target = self._sheet_targets.get(name)
        if target is None:
            raise KeyError(f"no sheet {name!r} in {self.path}")
        root = ET.fromstring(self._zip.read(target))
        sheet_data = root.find("m:sheetData", NS)
        raw_rows = []
        for row in sheet_data.findall("m:row", NS):
            row_num = int(row.get("r"))
            cells = {}
            for c in row.findall("m:c", NS):
                col = _col_letters(c.get("r"))
                val = _cell_value(c, self._shared_strings)
                if val is not None:
                    cells[col] = val
            raw_rows.append((row_num, cells))
        if not raw_rows:
            return []
        header_row_num, header_cells = raw_rows[0]
        header_by_col = {col: text for col, text in header_cells.items() if text}
        out = []
        for row_num, cells in raw_rows[1:]:
            if not cells:
                continue
            record = {
                header_by_col[col]: val
                for col, val in cells.items()
                if col in header_by_col
            }
            if not record:
                continue
            out.append((row_num, record))
        return out

    def close(self):
        self._zip.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
