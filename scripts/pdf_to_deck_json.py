#!/usr/bin/env python3
"""Convert a board deck PDF into a deck JSON structure for summarization."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None


def extract_lines(raw: str) -> list[str]:
    lines = []
    for line in raw.splitlines():
        normalized = re.sub(r"\s+", " ", line).strip()
        if normalized:
            if len(normalized) % 2 == 0:
                half_text = len(normalized) // 2
                if normalized[:half_text] == normalized[half_text:]:
                    normalized = normalized[:half_text]
            words = normalized.split(" ")
            if len(words) >= 4 and len(words) % 2 == 0:
                half = len(words) // 2
                if words[:half] == words[half:]:
                    normalized = " ".join(words[:half])
            lines.append(normalized)
    return lines


def derive_title(page_text: str) -> str:
    for line in page_text.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate[:200]
    return "(untitled)"


def boilerplate_lines(pages: list[list[str]]) -> set[str]:
    """Find repeated short lines likely to be headers/footers/watermarks."""
    counts: dict[str, int] = {}
    for page_lines in pages:
        seen = set(page_lines)
        for line in seen:
            if len(line) <= 90:
                counts[line] = counts.get(line, 0) + 1

    min_count = max(2, math.ceil(len(pages) * 0.4))
    return {line for line, count in counts.items() if count >= min_count}


def clean_page_lines(lines: list[str], page_number: int, repeated: set[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        if line in repeated:
            continue
        if line.isdigit() and int(line) == page_number:
            continue
        if cleaned and cleaned[-1] == line:
            continue
        cleaned.append(line)
    return cleaned


def convert_pdf(pdf_path: Path) -> dict:
    if PdfReader is None:
        raise RuntimeError(
            "Missing dependency: pypdf. Install with `python -m pip install pypdf`."
        )
    reader = PdfReader(str(pdf_path))
    raw_page_lines: list[list[str]] = []

    for page in reader.pages:
        extracted = page.extract_text() or ""
        raw_page_lines.append(extract_lines(extracted))

    repeated = boilerplate_lines(raw_page_lines)
    pages = []

    for index, lines in enumerate(raw_page_lines, start=1):
        cleaned_lines = clean_page_lines(lines, index, repeated)
        cleaned = "\n".join(cleaned_lines)
        title = derive_title(cleaned)

        pages.append(
            {
                "slide_number": index,
                "title": title,
                "text": cleaned,
                "notes": "",
            }
        )

    return {
        "source_pdf": str(pdf_path.name),
        "extracted_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "deck_pages": pages,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a PDF board deck into deck_pages JSON."
    )
    parser.add_argument("--pdf", required=True, help="Path to source PDF.")
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path. Defaults to ./data/decks/<pdf-name>.json",
    )
    return parser.parse_args()


def default_output_path(pdf_path: Path) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", pdf_path.stem.lower()).strip("_")
    return Path("data") / "decks" / f"{slug}.json"


def main() -> int:
    args = parse_args()
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"PDF not found: {pdf_path}", file=sys.stderr)
        return 1

    output_path = Path(args.output) if args.output else default_output_path(pdf_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        deck = convert_pdf(pdf_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to convert PDF: {exc}", file=sys.stderr)
        return 1
    output_path.write_text(json.dumps(deck, indent=2), encoding="utf-8")

    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
