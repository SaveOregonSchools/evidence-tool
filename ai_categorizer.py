"""Ollama-backed categorization and description for unique evidence files."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from common import EXTRACT_MAX_CHARS, OLLAMA_NUM_PREDICT
from ollama_client import OllamaEmptyResponseError, chat
from text_extract import extract_text

PATCH_ID = "2026-06-12-ollama-thinking-v3"


CATEGORIZATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {
            "type": "string",
            "description": "Exactly one concise category label. Prefer one of the supplied categories when categories are supplied.",
        },
        "description": {
            "type": "string",
            "description": "A 3-4 sentence, evidence-focused summary of the file contents, under 120 words total.",
        },
    },
    "required": ["category", "description"],
    "additionalProperties": False,
}


def _strip_model_wrappers(text: str) -> str:
    s = (text or "").strip()
    # Some reasoning models emit thinking tags in content even when asked not to.
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.IGNORECASE | re.DOTALL).strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    if s.lower().startswith("json\n"):
        s = s[5:].strip()
    return s


def _balanced_json_object(text: str) -> str | None:
    """Return the first complete JSON object in text, ignoring braces inside strings."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for pos in range(start, len(text)):
        ch = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : pos + 1]
    return None


def _clean_json_text(text: str) -> str:
    s = _strip_model_wrappers(text)
    balanced = _balanced_json_object(s)
    if balanced:
        return balanced.strip()
    # Fall back to broad extraction for simple prose-wrapped JSON.
    match = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if match:
        return match.group(0).strip()
    return s


def _json_unescape(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value.replace('\\"', '"').replace("\\n", "\n").strip()


def _regex_field(text: str, field: str) -> str:
    # JSON-ish: "category": "...". The closing quote is optional so partial JSON
    # still yields useful category/description fields instead of Uncategorized.
    pattern = rf'"{re.escape(field)}"\s*:\s*"(?P<value>(?:\\.|[^"\\])*)'
    match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return _json_unescape(match.group("value")).strip()

    # XML/tag-ish: <category>...</category>
    match = re.search(rf"<{re.escape(field)}>(?P<value>.*?)</{re.escape(field)}>", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group("value").strip()

    # Label-ish: Category: ... or Description: ...
    match = re.search(rf"^{re.escape(field)}\s*[:=-]\s*(?P<value>.+)$", text, flags=re.IGNORECASE | re.MULTILINE)
    if match:
        return match.group("value").strip().strip('"')

    # Some models write phrases such as "category tag:" or "description label:".
    match = re.search(
        rf"^{re.escape(field)}\s+(?:tag|label|field)\s*[:=-]\s*(?P<value>.+)$",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if match:
        return match.group("value").strip().strip('"')
    return ""


def _parse_response(text: str) -> dict[str, str]:
    raw = (text or "").strip()
    cleaned = _clean_json_text(raw)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            category = str(data.get("category") or data.get("category_tag") or data.get("category_label") or "Uncategorized").strip()
            description = str(data.get("description") or data.get("description_tag") or data.get("description_label") or "").strip()
            return {"category": category or "Uncategorized", "description": description, "status": "ok"}
    except json.JSONDecodeError:
        pass

    category = (
        _regex_field(raw, "category")
        or _regex_field(raw, "category_tag")
        or _regex_field(raw, "category_label")
        or _regex_field(cleaned, "category")
        or _regex_field(cleaned, "category_tag")
        or _regex_field(cleaned, "category_label")
    )
    description = (
        _regex_field(raw, "description")
        or _regex_field(raw, "description_tag")
        or _regex_field(raw, "description_label")
        or _regex_field(cleaned, "description")
        or _regex_field(cleaned, "description_tag")
        or _regex_field(cleaned, "description_label")
    )
    if category or description:
        return {
            "category": category or "Uncategorized",
            "description": description or raw[:4000],
            "status": "parsed_fields",
        }

    return {
        "category": "Uncategorized",
        "description": raw[:4000],
        "status": "parsed_as_text",
    }


def _categories_block(categories: str) -> str:
    items = [item.strip(" -\t") for item in re.split(r"[\n,;]+", categories or "") if item.strip(" -\t")]
    if not items:
        return "No fixed category list was supplied. Create one concise, useful category label."
    return (
        "Choose exactly one category from this list. Use 'Other / Unclear' only if none of the listed "
        "categories fit the file's actual content. Do not invent a new category when one of these fits:\n"
        + "\n".join(f"- {item}" for item in items)
    )


def build_messages(
    *,
    file_metadata: dict[str, Any],
    extracted_text: str,
    extraction_status: str,
    extraction_error: str,
    categories: str,
    context: str,
    extra_no_think_instruction: bool = False,
) -> list[dict[str, str]]:
    no_think_header = "/no_think\n" if extra_no_think_instruction else ""
    system = f"""
{no_think_header}You are a careful investigative evidence librarian. Your task is to triage one file for an investigator.

Rules:
- Use the project context only as background for disambiguating terms. Do not repeat the context as if it were evidence from this file.
- Focus on what the file itself appears to contain: document type, subject matter, named organizations or people, dates, decisions, agenda items, requests, evidence value, or notable contents.
- Do not write generic descriptions such as "This is from the Partnership for the Future of Learning" or "This file is associated with PFL" unless the extracted text, file name, or metadata specifically supports that as a meaningful description of the file.
- Prefer descriptions that start with the artifact type and contents, such as "A memo...", "A slide deck...", "A participant list...", "Meeting notes...", or "An image file...".
- If extracted text is unavailable, explicitly say that text extraction was unavailable or failed, then make the best careful inference from filename, type, folder, dates, and size.
- If the file appears unrelated to the project context, say that briefly and still summarize what the file appears to contain.
- Do not invent people, organizations, dates, claims, or legal significance that are not supported by the provided material.
- Do not make legal conclusions.
- Final answer only: no reasoning trace, no chain-of-thought, no hidden reasoning, no markdown, no code fence, no XML tags, no introductory prose.
- Return compact valid JSON only.
- Begin the answer with {{ and end it with }}.
- JSON schema: {{"category": "one category string", "description": "3-4 concise evidence-focused sentences, under 120 words total"}}
""".strip()
    user = f"""
Investigation / project context, for background only:
{context or '[No context supplied.]'}

Category instructions:
{_categories_block(categories)}

File metadata:
{json.dumps(file_metadata, indent=2, ensure_ascii=False)}

Extraction status: {extraction_status}
Extraction error, if any: {extraction_error or '[none]'}

Extracted text, capped for local model context:
{extracted_text or '[No extracted text available; use metadata only.]'}

Return the JSON object only now. /no_think
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _chat_with_fallbacks(messages: list[dict[str, str]], *, model: str | None) -> str:
    """Try structured output first, then simpler prompts if a thinking model returns empty content."""
    attempts: list[tuple[str, str | dict[str, Any] | None, int]] = [
        ("json_schema", CATEGORIZATION_JSON_SCHEMA, OLLAMA_NUM_PREDICT),
        ("json_mode", "json", OLLAMA_NUM_PREDICT),
        ("plain_json", None, max(OLLAMA_NUM_PREDICT, 4096)),
    ]
    errors: list[str] = []
    for label, response_format, num_predict in attempts:
        try:
            return chat(
                messages,
                model=model,
                temperature=0.0,
                response_format=response_format,
                num_predict=num_predict,
                think=False,
            )
        except OllamaEmptyResponseError as e:
            errors.append(f"{label}: {e}")
            # Continue to simpler response formats; some model/Ollama combinations
            # behave better without structured-output mode.
            continue
        except Exception:
            # Non-empty-response errors such as connection errors should be surfaced
            # immediately so the queue can pause/fail instead of doing needless work.
            raise

    raise OllamaEmptyResponseError(
        "Ollama returned thinking traces but no final assistant content after schema, json, and plain JSON attempts.\n"
        + "\n\n".join(errors[-3:]),
        had_thinking=True,
    )


def categorize_file(
    *,
    path: str | Path,
    file_metadata: dict[str, Any],
    categories: str,
    context: str,
    model: str | None = None,
    max_chars: int = EXTRACT_MAX_CHARS,
) -> dict[str, Any]:
    extraction = extract_text(path, max_chars=max_chars)
    messages = build_messages(
        file_metadata=file_metadata,
        extracted_text=str(extraction.get("text") or ""),
        extraction_status=str(extraction.get("status") or ""),
        extraction_error=str(extraction.get("error") or ""),
        categories=categories,
        context=context,
        extra_no_think_instruction=True,
    )

    raw = _chat_with_fallbacks(messages, model=model)
    parsed = _parse_response(raw)
    parsed["raw_response"] = raw
    parsed["extraction_status"] = extraction.get("status")
    parsed["extraction_error"] = extraction.get("error")
    parsed["extracted_chars"] = extraction.get("chars", 0)
    return parsed
