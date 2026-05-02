#!/usr/bin/env python3
"""Convert an XLSX workbook into deck_pages JSON for context ingestion."""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def col_letters(cell_ref: str) -> str:
    match = re.match(r"([A-Z]+)", cell_ref)
    return match.group(1) if match else cell_ref


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    ns = {"m": MAIN_NS}
    values: list[str] = []
    for si in root.findall("m:si", ns):
        text_parts: list[str] = []
        for t_node in si.findall(".//m:t", ns):
            text_parts.append(t_node.text or "")
        values.append("".join(text_parts))
    return values


def workbook_sheets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    ns = {"m": MAIN_NS, "r": REL_NS, "pr": PKG_REL_NS}
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))

    rel_map: dict[str, str] = {}
    for rel in rels.findall("pr:Relationship", ns):
        rel_id = rel.get("Id")
        target = rel.get("Target")
        if rel_id and target:
            rel_map[rel_id] = target

    sheets: list[tuple[str, str]] = []
    sheets_node = wb.find("m:sheets", ns)
    if sheets_node is None:
        return sheets

    for sheet in sheets_node.findall("m:sheet", ns):
        name = sheet.get("name", "Sheet")
        rel_id = sheet.get(f"{{{REL_NS}}}id", "")
        target = rel_map.get(rel_id, "")
        if target:
            sheets.append((name, f"xl/{target}"))
    return sheets


def normalize_value(raw: str) -> str:
    value = re.sub(r"\s+", " ", raw).strip()
    return value


def read_sheet_rows(zf: zipfile.ZipFile, sheet_path: str, shared: list[str]) -> list[str]:
    ns = {"m": MAIN_NS}
    root = ET.fromstring(zf.read(sheet_path))
    sheet_data = root.find("m:sheetData", ns)
    if sheet_data is None:
        return []

    lines: list[str] = []
    for row in sheet_data.findall("m:row", ns):
        cells: dict[str, str] = {}
        for cell in row.findall("m:c", ns):
            ref = cell.get("r", "")
            col = col_letters(ref)
            t = cell.get("t")
            v_node = cell.find("m:v", ns)
            if v_node is None or v_node.text is None:
                continue
            raw = v_node.text
            if t == "s":
                try:
                    idx = int(raw)
                except ValueError:
                    value = raw
                else:
                    value = shared[idx] if 0 <= idx < len(shared) else raw
            else:
                value = raw
            value = normalize_value(value)
            if value:
                cells[col] = value

        if not cells:
            continue

        ordered_cols = sorted(cells.keys(), key=lambda x: (len(x), x))
        parts = [cells[col] for col in ordered_cols]
        line = " | ".join(parts).strip()
        if line:
            lines.append(line)
    return lines


def convert_xlsx(xlsx_path: Path) -> dict:
    with zipfile.ZipFile(xlsx_path) as zf:
        shared = load_shared_strings(zf)
        sheets = workbook_sheets(zf)
        pages: list[dict] = []
        for index, (sheet_name, sheet_path) in enumerate(sheets, start=1):
            if sheet_path not in zf.namelist():
                continue
            lines = read_sheet_rows(zf, sheet_path, shared)
            text = "\n".join(lines)
            pages.append(
                {
                    "slide_number": index,
                    "title": sheet_name,
                    "text": text,
                    "notes": "",
                }
            )

    if not pages:
        raise ValueError("No readable sheets found in workbook.")

    return {
        "source_xlsx": str(xlsx_path.name),
        "extracted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deck_pages": pages,
    }


def default_output_path(xlsx_path: Path) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", xlsx_path.stem.lower()).strip("_")
    return Path("data") / "decks" / f"{slug}.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert an XLSX workbook into deck_pages JSON."
    )
    parser.add_argument("--xlsx", required=True, help="Path to source XLSX workbook.")
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path. Defaults to ./data/decks/<xlsx-name>.json",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    xlsx_path = Path(args.xlsx)
    if not xlsx_path.exists():
        print(f"XLSX not found: {xlsx_path}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else default_output_path(xlsx_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        deck = convert_xlsx(xlsx_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to convert XLSX: {exc}", file=sys.stderr)
        return 1

    output_path.write_text(json.dumps(deck, indent=2), encoding="utf-8")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
