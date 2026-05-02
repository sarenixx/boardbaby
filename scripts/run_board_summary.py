#!/usr/bin/env python3
"""Run the board deck multi-agent summarization flow from board materials.

Example:
  python scripts/run_board_summary.py \
    --deck "materials/acme_board_meeting.pdf" \
    --context-deck "materials/acme_financial_model.xlsx" \
    --factor-4-name "Burn Multiple" \
    --factor-5-name "Net Dollar Retention" \
    --output-json outputs/board_summary.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error, request

from pdf_to_deck_json import convert_pdf
from xlsx_to_deck_json import convert_xlsx

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts" / "agents"
SYSTEM_MESSAGE = (
    "You are a precise board-deck analysis assistant. "
    "Follow the output format exactly and avoid extra text."
)
DEFAULT_PRIMARY_MAX_SLIDES = 30
DEFAULT_CONTEXT_MAX_SLIDES = 12
DEFAULT_PRIMARY_TOTAL_CHARS = 32000
DEFAULT_CONTEXT_TOTAL_CHARS = 14000
DEFAULT_SLIDE_TEXT_CHARS = 2200


@dataclass
class Slide:
    slide_number: int
    title: str
    text: str
    notes: str


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_template(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing prompt template: {path}")
    return path.read_text(encoding="utf-8")


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def load_material(path: Path) -> tuple[dict[str, Any], str]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = load_json(path)
        if isinstance(data, dict):
            return data, "json"
        if isinstance(data, list):
            return {"deck_pages": data}, "json"
        raise ValueError("JSON material must be an object or list of slides.")

    if suffix == ".pdf":
        return convert_pdf(path), "pdf"

    if suffix in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        return convert_xlsx(path), "xlsx"

    raise ValueError(
        f"Unsupported material type for {path}. Supported extensions: .json, .pdf, .xlsx"
    )


def normalize_slides(data: Any) -> list[Slide]:
    if isinstance(data, list):
        raw_slides = data
    elif isinstance(data, dict):
        raw_slides = data.get("deck_pages")
        if raw_slides is None:
            raw_slides = data.get("slides")
    else:
        raw_slides = None

    if not isinstance(raw_slides, list) or not raw_slides:
        raise ValueError(
            "Deck JSON must be a non-empty list or an object with `deck_pages`/`slides`."
        )

    slides: list[Slide] = []
    for index, raw in enumerate(raw_slides, start=1):
        if not isinstance(raw, dict):
            continue

        raw_number = raw.get("slide_number", raw.get("page", raw.get("slide", index)))
        try:
            slide_number = int(raw_number)
        except (TypeError, ValueError):
            slide_number = index

        title = str(raw.get("title", raw.get("heading", ""))).strip()

        text_fields = [raw.get("text"), raw.get("body"), raw.get("content")]
        text = "\n".join(str(x).strip() for x in text_fields if x is not None and str(x).strip())

        notes = str(raw.get("notes", raw.get("speaker_notes", ""))).strip()

        slides.append(Slide(slide_number=slide_number, title=title, text=text, notes=notes))

    if not slides:
        raise ValueError("No usable slide entries found in deck JSON.")

    return slides


def format_deck_text(slides: list[Slide]) -> str:
    chunks: list[str] = []
    for slide in slides:
        lines = [f"Slide {slide.slide_number}", f"Title: {slide.title or '(untitled)'}"]
        if slide.text:
            lines.append("Text:")
            lines.append(slide.text)
        if slide.notes:
            lines.append("Notes:")
            lines.append(slide.notes)
        chunks.append("\n".join(lines))
    return "\n\n---\n\n".join(chunks)


def compact_slides(
    slides: list[Slide],
    max_slides: int,
    max_total_chars: int,
    per_slide_text_chars: int,
) -> list[Slide]:
    """Trim deck size to control prompt tokens while preserving early-slide signal."""
    limited = slides[:max_slides]
    compacted: list[Slide] = []
    consumed = 0

    for slide in limited:
        text = slide.text
        if len(text) > per_slide_text_chars:
            text = text[:per_slide_text_chars].rstrip() + "\n[TRUNCATED]"

        remaining = max_total_chars - consumed
        if remaining <= 0:
            break

        if len(text) > remaining:
            text = text[:remaining].rstrip() + "\n[TRUNCATED]"

        compacted.append(
            Slide(
                slide_number=slide.slide_number,
                title=slide.title,
                text=text,
                notes=slide.notes,
            )
        )
        consumed += len(text)

    return compacted


def extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())

    return "\n".join(chunks).strip()


def extract_retry_seconds(message: str) -> float:
    match = re.search(r"Please try again in ([0-9]+(?:\\.[0-9]+)?)s", message)
    if not match:
        return 10.0
    try:
        return float(match.group(1)) + 1.0
    except ValueError:
        return 10.0


def call_openai_responses(
    prompt: str,
    model: str,
    api_key: str,
    base_url: str,
    max_retries: int,
) -> str:
    url = base_url.rstrip("/") + "/responses"
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_MESSAGE}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    }

    payload = json.dumps(body).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    for attempt in range(max_retries + 1):
        try:
            with request.urlopen(req, timeout=180) as resp:
                resp_body = resp.read().decode("utf-8")
            break
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            is_rate_limit = exc.code == 429 and "rate_limit" in detail.lower()
            if is_rate_limit and attempt < max_retries:
                wait_seconds = extract_retry_seconds(detail)
                time.sleep(wait_seconds)
                continue
            raise RuntimeError(f"OpenAI API error ({exc.code}): {detail}") from exc
        except error.URLError as exc:
            if attempt < max_retries:
                time.sleep(2.0)
                continue
            raise RuntimeError(f"Network error calling OpenAI API: {exc}") from exc

    try:
        data = json.loads(resp_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON API response: {resp_body[:500]}") from exc

    text = extract_response_text(data)
    if not text:
        raise RuntimeError("Model returned no text output.")

    return text


def parse_json_output(raw: str) -> dict[str, Any]:
    text = raw.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\\s*```$", "", text)

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed

    raise ValueError(f"Could not parse JSON output from model:\n{raw}")


def count_words(text: str) -> int:
    return len(re.findall(r"\\b[\\w'-]+\\b", text))


def run_pipeline(
    deck_path: Path,
    context_deck_path: Path | None,
    factor_4_name: str,
    factor_5_name: str,
    model: str,
    api_key: str,
    base_url: str,
    max_retries: int,
    primary_max_slides: int,
    context_max_slides: int,
    primary_max_total_chars: int,
    context_max_total_chars: int,
    per_slide_text_chars: int,
    dry_run: bool,
) -> dict[str, Any]:
    raw_deck, primary_material_type = load_material(deck_path)
    primary_slides = normalize_slides(raw_deck)
    primary_slides_for_prompt = compact_slides(
        primary_slides,
        max_slides=primary_max_slides,
        max_total_chars=primary_max_total_chars,
        per_slide_text_chars=per_slide_text_chars,
    )
    primary_deck_text = format_deck_text(primary_slides_for_prompt)

    context_slides: list[Slide] = []
    context_slides_for_prompt: list[Slide] = []
    context_deck_text = "(No secondary context deck provided.)"
    context_material_type = ""
    if context_deck_path is not None:
        raw_context_deck, context_material_type = load_material(context_deck_path)
        context_slides = normalize_slides(raw_context_deck)
        context_slides_for_prompt = compact_slides(
            context_slides,
            max_slides=context_max_slides,
            max_total_chars=context_max_total_chars,
            per_slide_text_chars=per_slide_text_chars,
        )
        context_deck_text = format_deck_text(context_slides_for_prompt)

    relevance_template = load_template("relevance_agent.md")
    metrics_template = load_template("metrics_agent.md")
    context_template = load_template("context_agent.md")
    synthesis_template = load_template("synthesis_agent.md")

    relevance_prompt = render_template(
        relevance_template,
        {"deck_text": primary_deck_text, "context_deck_text": context_deck_text},
    )

    if dry_run:
        return {
            "mode": "dry_run",
            "primary_slides_parsed": len(primary_slides),
            "context_slides_parsed": len(context_slides),
            "primary_slides_in_prompt": len(primary_slides_for_prompt),
            "context_slides_in_prompt": len(context_slides_for_prompt),
            "primary_material_path": str(deck_path),
            "primary_material_type": primary_material_type,
            "context_material_path": str(context_deck_path) if context_deck_path else "",
            "context_material_type": context_material_type,
            "prompts": {
                "relevance": relevance_prompt,
                "metrics": "(generated after relevance output)",
                "context": "(generated after relevance output)",
                "synthesis": "(generated after metrics/context outputs)",
            },
        }

    relevance_raw = call_openai_responses(
        relevance_prompt, model, api_key, base_url, max_retries=max_retries
    )
    relevance_json = parse_json_output(relevance_raw)

    selected_slides_json = json.dumps(relevance_json.get("selected_slides", []), indent=2)

    metrics_prompt = render_template(
        metrics_template,
        {
            "factor_4_name": factor_4_name,
            "factor_5_name": factor_5_name,
            "selected_slides_json": selected_slides_json,
            "deck_text": primary_deck_text,
            "context_deck_text": context_deck_text,
        },
    )
    metrics_raw = call_openai_responses(
        metrics_prompt, model, api_key, base_url, max_retries=max_retries
    )
    metrics_json = parse_json_output(metrics_raw)

    context_prompt = render_template(
        context_template,
        {
            "selected_slides_json": selected_slides_json,
            "deck_text": primary_deck_text,
            "context_deck_text": context_deck_text,
        },
    )
    context_raw = call_openai_responses(
        context_prompt, model, api_key, base_url, max_retries=max_retries
    )
    context_json = parse_json_output(context_raw)

    synthesis_prompt = render_template(
        synthesis_template,
        {
            "factor_4_name": factor_4_name,
            "factor_5_name": factor_5_name,
            "relevance_json": json.dumps(relevance_json, indent=2),
            "metrics_json": json.dumps(metrics_json, indent=2),
            "context_json": json.dumps(context_json, indent=2),
        },
    )
    final_paragraph = call_openai_responses(
        synthesis_prompt, model, api_key, base_url, max_retries=max_retries
    ).strip()

    words = count_words(final_paragraph)
    if words < 100 or words > 150:
        retry_prompt = synthesis_prompt + (
            "\n\nRevision request: rewrite the paragraph to be between 100 and 150 words, "
            "single paragraph only, no extra text."
        )
        final_paragraph = call_openai_responses(
            retry_prompt, model, api_key, base_url, max_retries=max_retries
        ).strip()

    return {
        "mode": "live",
        "primary_slides_parsed": len(primary_slides),
        "context_slides_parsed": len(context_slides),
        "primary_slides_in_prompt": len(primary_slides_for_prompt),
        "context_slides_in_prompt": len(context_slides_for_prompt),
        "primary_material_path": str(deck_path),
        "primary_material_type": primary_material_type,
        "context_material_path": str(context_deck_path) if context_deck_path else "",
        "context_material_type": context_material_type,
        "factor_4_name": factor_4_name,
        "factor_5_name": factor_5_name,
        "model": model,
        "relevance": relevance_json,
        "metrics": metrics_json,
        "context": context_json,
        "final_paragraph": final_paragraph,
        "final_word_count": count_words(final_paragraph),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run multi-agent board deck summarization.")
    parser.add_argument(
        "--deck",
        required=True,
        help="Path to PRIMARY material file (.json, .pdf, .xlsx).",
    )
    parser.add_argument(
        "--context-deck",
        default="",
        help="Optional path to SECONDARY context material (.json, .pdf, .xlsx), kept separate.",
    )
    parser.add_argument("--factor-4-name", required=True, help="Name for metric factor 4.")
    parser.add_argument("--factor-5-name", required=True, help="Name for metric factor 5.")
    parser.add_argument("--model", default="gpt-4.1", help="Model name for OpenAI Responses API.")
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Retries for rate limits/network issues per model call.",
    )
    parser.add_argument(
        "--primary-max-slides",
        type=int,
        default=DEFAULT_PRIMARY_MAX_SLIDES,
        help="Maximum number of primary slides included in prompts.",
    )
    parser.add_argument(
        "--context-max-slides",
        type=int,
        default=DEFAULT_CONTEXT_MAX_SLIDES,
        help="Maximum number of context slides included in prompts.",
    )
    parser.add_argument(
        "--primary-max-total-chars",
        type=int,
        default=DEFAULT_PRIMARY_TOTAL_CHARS,
        help="Character budget for primary deck text in prompts.",
    )
    parser.add_argument(
        "--context-max-total-chars",
        type=int,
        default=DEFAULT_CONTEXT_TOTAL_CHARS,
        help="Character budget for context deck text in prompts.",
    )
    parser.add_argument(
        "--per-slide-text-chars",
        type=int,
        default=DEFAULT_SLIDE_TEXT_CHARS,
        help="Maximum characters per slide text before truncation.",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.openai.com/v1",
        help="Base URL for OpenAI-compatible Responses API.",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional path to save full intermediate + final outputs as JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and return prompt scaffolding without API calls.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    deck_path = Path(args.deck)
    if not deck_path.exists():
        print(f"Deck file not found: {deck_path}", file=sys.stderr)
        return 1

    context_deck_path: Path | None = None
    if args.context_deck:
        context_deck_path = Path(args.context_deck)
        if not context_deck_path.exists():
            print(f"Context deck file not found: {context_deck_path}", file=sys.stderr)
            return 1

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not args.dry_run and not api_key:
        print(
            "OPENAI_API_KEY is required for live runs. Use --dry-run to verify prompt setup.",
            file=sys.stderr,
        )
        return 1

    try:
        result = run_pipeline(
            deck_path=deck_path,
            context_deck_path=context_deck_path,
            factor_4_name=args.factor_4_name,
            factor_5_name=args.factor_5_name,
            model=args.model,
            api_key=api_key,
            base_url=args.base_url,
            max_retries=args.max_retries,
            primary_max_slides=args.primary_max_slides,
            context_max_slides=args.context_max_slides,
            primary_max_total_chars=args.primary_max_total_chars,
            context_max_total_chars=args.context_max_total_chars,
            per_slide_text_chars=args.per_slide_text_chars,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should surface clean errors.
        print(f"Pipeline failed: {exc}", file=sys.stderr)
        return 1

    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    if args.dry_run:
        print(json.dumps(result, indent=2))
        return 0

    print(result["final_paragraph"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
