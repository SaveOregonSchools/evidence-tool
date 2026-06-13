"""Ollama-backed categorization and description for unique evidence files."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from common import (
    AI_SEND_IMAGES,
    CATEGORY_DEFINITIONS_TEXT,
    CATEGORY_PRECEDENCE_RULES_TEXT,
    DEFAULT_PRIMARY_CATEGORIES,
    DEFAULT_PROJECT_CONTEXT,
    EVIDENCE_BASIS_VALUES,
    EXTRACT_MAX_CHARS,
    IMAGE_MAX_BYTES,
    OLLAMA_NUM_PREDICT,
)
from ollama_client import OllamaEmptyResponseError, chat
from text_extract import IMAGE_EXTS, extract_text

PATCH_ID = "2026-06-13-ui-category-confidence-v1"


# Older evidence-tool runs and some copied prompt text use labels that are
# close to, but not exactly, the current recommended primary categories.
# Normalize those labels before validating model output so that a useful
# category is not thrown away merely because the category box contained old
# labels, bullets, definitions, trailing colons, or copied markdown.
CATEGORY_ALIASES: dict[str, str] = {
    "Network convening / assembly materials": "Network assembly / convening materials",
    "Network assembly / convening": "Network assembly / convening materials",
    "Network assembly materials": "Network assembly / convening materials",
    "Participant or member lists": "Participant / attendee lists",
    "Participant lists": "Participant / attendee lists",
    "Attendee lists": "Participant / attendee lists",
    "Participant list": "Participant / attendee lists",
    "People / organization list": "People / organization directory",
    "People / organizations directory": "People / organization directory",
    "People / org directory": "People / organization directory",
    "People / organization lists": "People / organization directory",
    "Strategy / governance / planning": "Network strategy / governance / regeneration",
    "Network strategy / governance / planning": "Network strategy / governance / regeneration",
    "Strategy / governance / regeneration": "Network strategy / governance / regeneration",
    "Strategy regeneration": "Network strategy / governance / regeneration",
    "Workgroup / priority-area planning": "Work group / priority-area planning",
    "Work group planning": "Work group / priority-area planning",
    "Priority-area planning": "Work group / priority-area planning",
    "Policy framework or policy agenda": "Policy framework / policy agenda",
    "Policy framework and policy agenda": "Policy framework / policy agenda",
    "Shared Story / narrative / communications": "Shared Story / narrative / communications strategy",
    "Shared Story / narrative strategy": "Shared Story / narrative / communications strategy",
    "Narrative / communications strategy": "Shared Story / narrative / communications strategy",
    "Education resourcing": "Education resourcing / school funding",
    "School funding": "Education resourcing / school funding",
    "Teacher workforce": "Teacher / educator workforce",
    "Educator workforce": "Teacher / educator workforce",
    "Place-based strategy": "Place-based / Key Places strategy",
    "Key Places strategy": "Place-based / Key Places strategy",
    "Administrative / run-of-show / agenda / notes": "Administrative logistics / internal run-of-show",
    "Administrative / run-of-show": "Administrative logistics / internal run-of-show",
    "Administrative logistics": "Administrative logistics / internal run-of-show",
    "Run-of-show / agenda / notes": "Administrative logistics / internal run-of-show",
    "General announcement / member update": "General network announcement / member update",
    "General network announcement": "General network announcement / member update",
    "Member update": "General network announcement / member update",
    "Other / Unclear": "Unrelated or insufficient evidence",
    "Other": "Unrelated or insufficient evidence",
    "Unclear": "Unrelated or insufficient evidence",
    "Miscellaneous": "Unrelated or insufficient evidence",
}


def _strip_wrapping_punctuation(value: str) -> str:
    chars = "` \t\r\n\\\"'.,;:-–—“”‘’"
    return str(value or "").strip().strip(chars)


def _norm_category_text(value: str) -> str:
    s = str(value or "").strip()
    # Markdown bullets, numbering, and checkboxes.
    s = re.sub(r"^[-*•\u2022\s]+", "", s).strip()
    s = re.sub(r"^\d+[.)]\s+", "", s).strip()
    s = re.sub(r"^\[[ xX]\]\s+", "", s).strip()
    s = _strip_wrapping_punctuation(s)

    # If a pasted definition line starts with a category label, keep the label.
    # Example: "Community schools: Use for documents primarily about..."
    for cat in DEFAULT_PRIMARY_CATEGORIES:
        if re.match(rf"^{re.escape(cat)}\s*[:\-–—]", s, flags=re.IGNORECASE):
            return cat
    for alias, canonical in CATEGORY_ALIASES.items():
        if re.match(rf"^{re.escape(alias)}\s*[:\-–—]", s, flags=re.IGNORECASE):
            return canonical
    return _strip_wrapping_punctuation(s)


def _alias_to_default(value: str) -> str | None:
    candidate = _norm_category_text(value)
    folded = re.sub(r"\s+", " ", candidate).casefold()
    for cat in DEFAULT_PRIMARY_CATEGORIES:
        if re.sub(r"\s+", " ", cat).casefold() == folded:
            return cat
    for alias, canonical in CATEGORY_ALIASES.items():
        if re.sub(r"\s+", " ", alias).casefold() == folded:
            return canonical
    return None


def _split_category_definition(line: str) -> tuple[str, str]:
    """Return (category, definition) from a single line, if a definition is present."""
    raw = str(line or "").strip()
    raw = re.sub(r"^[-*•\u2022\s]+", "", raw).strip()
    raw = re.sub(r"^\d+[.)]\s+", "", raw).strip()
    raw = re.sub(r"^\[[ xX]\]\s+", "", raw).strip()
    if not raw:
        return "", ""

    # Prefer a colon delimiter because that is the user-facing format:
    # Category Name: Category definition
    for sep in (":", " - ", " – ", " — "):
        if sep in raw:
            left, right = raw.split(sep, 1)
            category = _norm_category_text(left)
            definition = str(right or "").strip()
            if category and definition:
                canonical = _alias_to_default(category) or category
                return canonical, definition

    cleaned = _norm_category_text(raw)
    return cleaned, ""


def _is_prompt_or_heading_line(value: str) -> bool:
    lower = str(value or "").strip().casefold().rstrip(":")
    if not lower:
        return True
    if lower in {
        "allowed primary categories",
        "recommended primary categories",
        "category definitions",
        "classification precedence rules",
        "task",
        "important rules",
        "return exactly this json structure",
    }:
        return True
    return lower.startswith((
        "use for ",
        "do not use ",
        "choose exactly ",
        "return ",
        "task:",
        "important rules",
        "classification precedence",
        "project context",
    ))


def _category_specs(categories: str) -> tuple[list[str], dict[str, str]]:
    """Parse user category input into allowed names plus optional definitions.

    Supported formats:
    - one category per line
    - Category Name: definition
    - CSV upload converted by app.py into Category Name: definition lines
    - older category aliases, which are normalized to the current default labels
    """
    raw = categories or ""
    if not raw.strip():
        return list(DEFAULT_PRIMARY_CATEGORIES), {}

    default_hits = []
    for cat in DEFAULT_PRIMARY_CATEGORIES:
        if re.search(rf"(^|[\n\r\-•*\s]){re.escape(cat)}\s*(:|$|[\n\r])", raw, flags=re.IGNORECASE):
            default_hits.append(cat)
    defaultish_markers = (
        "allowed primary categor" in raw.casefold()
        or "recommended primary categor" in raw.casefold()
        or "category definitions" in raw.casefold()
        or "classification precedence" in raw.casefold()
        or "choose exactly one primary" in raw.casefold()
    )

    # When the pasted text looks like the full recommended prompt, keep the clean
    # default category set. We still harvest one-line definitions if the user has
    # provided them in Category: definition form.
    force_defaults = (defaultish_markers and len(default_hits) >= 3) or len(default_hits) >= 8

    items: list[str] = []
    definitions: dict[str, str] = {}

    # Definition lines may contain commas and semicolons, so treat newlines as
    # primary delimiters. Only split comma/semicolon lists when a line has no
    # category-definition delimiter.
    raw_lines = [line.strip() for line in raw.replace("\r", "\n").split("\n")]
    pieces: list[str] = []
    for line in raw_lines:
        if not line.strip():
            continue
        stripped = line.strip()
        has_definition_delimiter = any(sep in stripped for sep in (":", " - ", " – ", " — "))
        if has_definition_delimiter:
            pieces.append(stripped)
        else:
            pieces.extend(part.strip() for part in re.split(r"[,;]+", stripped) if part.strip())

    for part in pieces:
        category, definition = _split_category_definition(part)
        if not category or _is_prompt_or_heading_line(category):
            continue
        canonical_default = _alias_to_default(category)
        category = canonical_default or category
        if category not in items:
            items.append(category)
        if definition:
            definitions[category] = definition

    if force_defaults:
        # Use default categories in their canonical order. Keep any user-supplied
        # definitions that match those categories.
        allowed = list(DEFAULT_PRIMARY_CATEGORIES)
        return allowed, {cat: definitions[cat] for cat in allowed if cat in definitions}

    return (items or list(DEFAULT_PRIMARY_CATEGORIES)), definitions


def _allowed_categories(categories: str) -> list[str]:
    return _category_specs(categories)[0]


def normalize_categories_text(categories: str) -> str:
    """Return the cleaned category list/spec that the app should save/use for a job."""
    allowed, definitions = _category_specs(categories)
    lines = []
    for item in allowed:
        definition = definitions.get(item, "").strip()
        if definition:
            lines.append(f"{item}: {definition}")
        else:
            lines.append(item)
    return "\n".join(lines)

def canonicalize_category(value: str, categories: str = "") -> str | None:
    """Public helper used by repair scripts and tests."""
    return _canonical_category(value, _allowed_categories(categories))


def _schema_for_categories(allowed_categories: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "primary_category": {
                "type": "string",
                "enum": allowed_categories,
                "description": "Exactly one value from the allowed primary category list.",
            },
            "secondary_tags": {
                "type": ["array", "string"],
                "items": {"type": "string"},
                "description": "Optional; up to three concise topical tags.",
            },
            "confidence": {"type": ["number", "string"], "description": "Decimal confidence from 0.00 to 1.00."},
            "description": {"type": "string", "description": "3-4 concise evidence-focused sentences."},
            "evidence_basis": {"type": "string", "enum": EVIDENCE_BASIS_VALUES},
            "key_people": {"type": ["array", "string"], "items": {"type": "string"}},
            "key_organizations": {"type": ["array", "string"], "items": {"type": "string"}},
            "date_or_event": {"type": "string"},
            "why_useful_as_evidence": {"type": "string"},
            "needs_human_review": {"type": ["boolean", "string"]},
        },
        "required": [
            "primary_category",
            "secondary_tags",
            "confidence",
            "description",
            "evidence_basis",
            "key_people",
            "key_organizations",
            "date_or_event",
            "why_useful_as_evidence",
            "needs_human_review",
        ],
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
    aliases = [field]
    if field == "primary_category":
        aliases.extend(["category", "category_tag", "category_label"])
    for name in aliases:
        # JSON-ish: "field": "...". The closing quote is optional so partial JSON
        # still yields useful fields instead of throwing everything away.
        pattern = rf'"{re.escape(name)}"\s*:\s*"(?P<value>(?:\\.|[^"\\])*)'
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return _json_unescape(match.group("value")).strip()

        # XML/tag-ish: <field>...</field>
        match = re.search(rf"<{re.escape(name)}>(?P<value>.*?)</{re.escape(name)}>", text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group("value").strip()

        # Label-ish: Field: ...
        label = name.replace("_", r"[ _]")
        match = re.search(rf"^{label}\s*[:=-]\s*(?P<value>.+)$", text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group("value").strip().strip('"')

        # Some models write phrases such as "category tag:" or "description label:".
        match = re.search(
            rf"^{label}\s+(?:tag|label|field)\s*[:=-]\s*(?P<value>.+)$",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            return match.group("value").strip().strip('"')
    return ""


def _stringify_value(value: Any, *, max_items: int | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        vals = [str(v).strip() for v in value if str(v).strip()]
        if max_items is not None:
            vals = vals[:max_items]
        return "; ".join(vals)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def _parse_confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            f = float(value)
            return max(0.0, min(1.0, f))
        except Exception:
            return None
    s = str(value).strip().lower()
    if s in {"high", "high confidence"}:
        return 0.9
    if s in {"medium", "moderate", "medium confidence"}:
        return 0.6
    if s in {"low", "low confidence"}:
        return 0.3
    match = re.search(r"\d+(?:\.\d+)?", s)
    if match:
        f = float(match.group(0))
        if f > 1.0 and f <= 100.0:
            f = f / 100.0
        return max(0.0, min(1.0, f))
    return None


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in {"true", "1", "yes", "y", "review", "needs review", "human review"}


def _parse_response(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    cleaned = _clean_json_text(raw)
    data: dict[str, Any] = {}
    status = "ok"
    try:
        parsed_json = json.loads(cleaned)
        if isinstance(parsed_json, dict):
            data = parsed_json
    except json.JSONDecodeError:
        status = "parsed_fields"

    if not data:
        field_names = [
            "primary_category",
            "secondary_tags",
            "confidence",
            "description",
            "evidence_basis",
            "key_people",
            "key_organizations",
            "date_or_event",
            "why_useful_as_evidence",
            "needs_human_review",
        ]
        data = {name: _regex_field(raw, name) or _regex_field(cleaned, name) for name in field_names}
        if not any(str(v or "").strip() for v in data.values()):
            return {
                "primary_category": "Unrelated or insufficient evidence",
                "secondary_tags": "",
                "confidence": 0.0,
                "description": raw[:4000],
                "evidence_basis": "metadata only",
                "key_people": "",
                "key_organizations": "",
                "date_or_event": "",
                "why_useful_as_evidence": "",
                "needs_human_review": True,
                "status": "parsed_as_text",
            }

    # Accept legacy keys from earlier app versions.
    primary_category = data.get("primary_category") or data.get("category") or data.get("category_tag") or data.get("category_label") or ""
    description = data.get("description") or data.get("description_tag") or data.get("description_label") or ""
    return {
        "primary_category": _stringify_value(primary_category),
        "secondary_tags": _stringify_value(data.get("secondary_tags"), max_items=3),
        "confidence": _parse_confidence(data.get("confidence")),
        "description": _stringify_value(description),
        "evidence_basis": _stringify_value(data.get("evidence_basis")),
        "key_people": _stringify_value(data.get("key_people")),
        "key_organizations": _stringify_value(data.get("key_organizations")),
        "date_or_event": _stringify_value(data.get("date_or_event")),
        "why_useful_as_evidence": _stringify_value(data.get("why_useful_as_evidence")),
        "needs_human_review": _parse_bool(data.get("needs_human_review")),
        "status": status,
    }


def _categories_block(allowed_categories: list[str]) -> str:
    return (
        "Allowed primary categories. Choose exactly one primary_category from this list and do not modify the wording:\n"
        + "\n".join(f"- {item}" for item in allowed_categories)
    )


def _using_default_categories(allowed_categories: list[str]) -> bool:
    return allowed_categories == list(DEFAULT_PRIMARY_CATEGORIES)


def _category_guidance_block(allowed_categories: list[str], category_definitions: dict[str, str] | None = None) -> str:
    category_definitions = category_definitions or {}
    if category_definitions:
        lines = ["Custom category definitions:"]
        for category in allowed_categories:
            definition = str(category_definitions.get(category, "")).strip()
            if definition:
                lines.append(f"\n{category}:\n{definition}")
        lines.append(
            "\nUse the dominant purpose of the file to choose exactly one primary_category from the allowed list. "
            "Do not invent or modify category names."
        )
        if _using_default_categories(allowed_categories):
            lines.append("\n" + CATEGORY_PRECEDENCE_RULES_TEXT)
        return "\n".join(lines)

    if _using_default_categories(allowed_categories):
        return f"{CATEGORY_DEFINITIONS_TEXT}\n\n{CATEGORY_PRECEDENCE_RULES_TEXT}"
    return (
        "Custom category list supplied by the user. Use the dominant purpose of the file to choose exactly one "
        "primary_category from that supplied list. Do not invent or modify category names. If the categories are "
        "not mutually exclusive, choose the one that best reflects the file's primary purpose and evidentiary use."
    )


def build_messages(
    *,
    file_metadata: dict[str, Any],
    extracted_text: str,
    ocr_text: str,
    extraction_status: str,
    extraction_error: str,
    categories: str,
    context: str,
    allowed_categories: list[str],
    category_definitions: dict[str, str] | None = None,
    is_image: bool = False,
    image_attached: bool = False,
    image_attach_error: str = "",
    extra_no_think_instruction: bool = False,
) -> list[dict[str, Any]]:
    no_think_header = "/no_think\n" if extra_no_think_instruction else ""
    system = f"""
{no_think_header}You are a careful investigative evidence librarian. Your task is to triage one file for an investigator.

Rules:
- Use the project context only as background for disambiguating terms. Do not repeat the context as if it were evidence from this file.
- Focus on what the file itself appears to contain: document type, subject matter, named organizations or people, dates, decisions, agenda items, requests, evidence value, or notable contents.
- Do not write generic descriptions such as "This is from the Partnership for the Future of Learning" or "This file is associated with PFL" unless the extracted text, visible image content, file name, or metadata specifically supports that as a meaningful description of the file.
- Prefer descriptions that start with the artifact type and contents, such as "A memo...", "A slide deck...", "A participant list...", "Meeting notes...", "An image file...", or "Based on metadata only...".
- If extracted text is unavailable, explicitly say that text extraction was unavailable or failed, then make the best careful inference from filename, type, folder, dates, and size.
- If the file appears unrelated to the project context, say that briefly and still summarize what the file appears to contain.
- Do not invent people, organizations, dates, claims, or legal significance that are not supported by the provided material.
- Do not make legal conclusions.
- Calibrate confidence carefully. Use 0.95+ only when the category and summary are directly supported by strong extracted text or visible image text; use 0.75-0.94 for solid but partial support; use 0.50-0.74 for plausible inference; use below 0.50 when relying mainly on filename or metadata.
- For metadata-only or filename-only files, do not return confidence above 0.55. For image files, do not return confidence above 0.85 unless visible text clearly identifies the topic and purpose.
- Final answer only: no reasoning trace, no chain-of-thought, no hidden reasoning, no markdown, no code fence, no XML tags, no introductory prose.
- Return compact valid JSON only.
- Begin the answer with {{ and end it with }}.
""".strip()

    image_instruction = ""
    if is_image:
        image_instruction = f"""
Image-specific instruction:
This file is an image. Use visible text, logos, layout, people shown only when identifiable from filename/visible text, and filename to summarize it. If visible text or logos are available, do not say the description is based on metadata only.
For images with partner logos, coalition badges, social-media cards, screenshots, or campaign graphics, classify by the apparent campaign/topic/purpose rather than defaulting to People / organization directory. Use People / organization directory only when the image is mainly a broad roster, logo sheet, contact/directory artifact, or person/organization reference with no clearer topical campaign purpose.
If the visible text, filename, or layout suggests vouchers, privatization, education funding, book bans, anti-CRT/anti-DEI attacks, public education defense, or a campaign such as Truth in Education Funding, prefer Public education defense / voucher-privatization response when that is an allowed category.
If no visible text is available and the image is not attached or cannot be interpreted, say the description is based on metadata only or filename only, keep confidence low, and mark needs_human_review as true.
Image attached to Ollama request: {str(bool(image_attached)).lower()}
Image attach error, if any: {image_attach_error or '[none]'}
""".strip()

    user = f"""
Project context, for background only:
{context or DEFAULT_PROJECT_CONTEXT}

{_categories_block(allowed_categories)}

{_category_guidance_block(allowed_categories, category_definitions)}

File name:
{file_metadata.get('file_name') or ''}

File metadata:
{json.dumps(file_metadata, indent=2, ensure_ascii=False)}

Extraction status: {extraction_status}
Extraction error, if any: {extraction_error or '[none]'}

Extracted document text:
{extracted_text or '[No extracted document text available.]'}

Visible image text / OCR text, if any:
{ocr_text or '[No visible image/OCR text available.]'}

{image_instruction}

Task:
Classify and summarize this file.

Return exactly this JSON structure:
{{
  "primary_category": "exactly one allowed category",
  "secondary_tags": ["up to three short tags, or an empty array"],
  "confidence": 0.00,
  "description": "3-4 concise sentences describing what the file itself contains",
  "evidence_basis": "one of: extracted text, visible image text, metadata only, filename only, mixed",
  "key_people": "important people named in the file, or blank",
  "key_organizations": "important organizations named in the file, or blank",
  "date_or_event": "specific date, year, meeting, assembly, retreat, or event name if present",
  "why_useful_as_evidence": "concise neutral explanation of why the file may matter in an evidence set",
  "needs_human_review": false
}}

Important rules:
- Do not invent or modify category names.
- Do not merely say the file is “about PFL.” Explain what the file actually contains.
- Base the description on extracted text, visible image text, file metadata, or filename.
- Do not speculate beyond available evidence.
- If little or no text is available, say “Based on metadata only...” or “Based on filename only...” and mark needs_human_review as true.
- If visible image text is available, do not say the description is based on metadata only.
- Calibrate confidence; avoid 1.00 unless the file content makes the category unmistakable. Use lower confidence and needs_human_review=true for metadata-only, filename-only, ambiguous, or image-only results.
- Avoid loaded, accusatory, or conclusory language.

Return the JSON object only now. /no_think
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _chat_with_fallbacks(messages: list[dict[str, Any]], *, model: str | None, schema: dict[str, Any]) -> str:
    """Try structured output first, then simpler prompts if a model returns empty content."""
    attempts: list[tuple[str, str | dict[str, Any] | None, int]] = [
        ("json_schema", schema, OLLAMA_NUM_PREDICT),
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
            continue
        except Exception:
            raise

    raise OllamaEmptyResponseError(
        "Ollama returned thinking traces but no final assistant content after schema, json, and plain JSON attempts.\n"
        + "\n\n".join(errors[-3:]),
        had_thinking=True,
    )


def _canonical_category(value: str, allowed_categories: list[str]) -> str | None:
    candidate = _norm_category_text(value)
    if not candidate:
        return None

    # First validate against the job's allowed categories.
    if candidate in allowed_categories:
        return candidate
    folded = re.sub(r"\s+", " ", candidate).casefold()
    for allowed in allowed_categories:
        allowed_clean = _norm_category_text(allowed)
        if re.sub(r"\s+", " ", allowed_clean).casefold() == folded:
            return allowed_clean

    # Then map older labels / near matches to equivalent default categories.
    default_candidate = _alias_to_default(candidate)
    if default_candidate:
        for allowed in allowed_categories:
            allowed_default = _alias_to_default(allowed) or _norm_category_text(allowed)
            if allowed_default == default_candidate:
                return default_candidate if default_candidate in DEFAULT_PRIMARY_CATEGORIES else _norm_category_text(allowed)
        if default_candidate in DEFAULT_PRIMARY_CATEGORIES and set(DEFAULT_PRIMARY_CATEGORIES).issubset(set(allowed_categories)):
            return default_candidate

    return None


def _invalid_category_retry_messages(
    messages: list[dict[str, Any]],
    *,
    invalid_category: str,
    allowed_categories: list[str],
) -> list[dict[str, Any]]:
    instruction = (
        "You used an invalid primary_category: "
        + json.dumps(invalid_category, ensure_ascii=False)
        + "\nChoose exactly one primary_category from this allowed list and do not modify the wording:\n"
        + "\n".join(allowed_categories)
        + "\nReturn the full JSON object again with all required fields. Do not include any explanation."
    )
    return messages + [{"role": "user", "content": instruction}]


def _guess_evidence_basis(*, is_image: bool, image_attached: bool, extracted_text: str, ocr_text: str) -> str:
    if is_image and ocr_text.strip() and extracted_text.strip():
        return "mixed"
    if is_image and (ocr_text.strip() or image_attached):
        return "visible image text"
    if extracted_text.strip():
        return "extracted text"
    return "metadata only"


def _calibrate_confidence(
    confidence: float | None,
    *,
    basis: str,
    category_valid: bool,
    needs_review: bool,
    is_image: bool,
    image_attached: bool,
    extracted_text: str,
    ocr_text: str,
    extraction_status: str,
    primary_category: str,
    parse_status: str,
) -> float:
    """Apply conservative caps so model confidence is useful for triage.

    Models tend to overuse 1.00. This tool treats confidence as a practical
    review signal, not a mathematical probability, so it caps scores when the
    evidence basis is weaker or the result needs review.
    """
    if confidence is None:
        if not category_valid:
            confidence = 0.30
        elif basis in {"filename only"}:
            confidence = 0.35
        elif basis in {"metadata only"}:
            confidence = 0.45
        elif basis == "visible image text":
            confidence = 0.75
        elif basis == "mixed":
            confidence = 0.82
        else:
            confidence = 0.80

    conf = max(0.0, min(1.0, float(confidence)))
    caps: list[float] = [0.97]

    if not category_valid:
        caps.append(0.35)
    if parse_status in {"parsed_as_text", "invalid_category_fallback"}:
        caps.append(0.45)
    elif parse_status in {"parsed_fields"}:
        caps.append(0.90)

    if basis == "filename only":
        caps.append(0.50)
    elif basis == "metadata only":
        caps.append(0.55)
    elif basis == "visible image text":
        caps.append(0.85)
    elif basis == "mixed":
        caps.append(0.90)

    if is_image:
        if not (ocr_text.strip() or image_attached):
            caps.append(0.50)
        else:
            caps.append(0.85)

    if extraction_status in {"extract_error_metadata_only", "unsupported_metadata_only"}:
        caps.append(0.60)
    if extraction_status in {"image_ocr_unavailable", "image_ocr_error", "image_ocr_no_text"} and not image_attached:
        caps.append(0.50)

    if not extracted_text.strip() and not ocr_text.strip() and not image_attached:
        caps.append(0.55)

    if primary_category == "Unrelated or insufficient evidence":
        caps.append(0.70)

    if needs_review:
        caps.append(0.65)

    return round(min(conf, *caps), 2)


def _postprocess_fields(
    parsed: dict[str, Any],
    *,
    allowed_categories: list[str],
    original_category: str,
    category_valid: bool,
    is_image: bool,
    image_attached: bool,
    extracted_text: str,
    ocr_text: str,
    extraction_status: str,
) -> dict[str, Any]:
    basis = (parsed.get("evidence_basis") or "").strip()
    if basis not in EVIDENCE_BASIS_VALUES:
        basis = _guess_evidence_basis(is_image=is_image, image_attached=image_attached, extracted_text=extracted_text, ocr_text=ocr_text)
    parsed["evidence_basis"] = basis

    needs_review = bool(parsed.get("needs_human_review"))
    conf = parsed.get("confidence")

    # Apply review rules before the final confidence cap so review-triggering
    # conditions cap high model scores.
    if basis in {"metadata only", "filename only"}:
        needs_review = True
    if is_image and not (ocr_text.strip() or image_attached):
        needs_review = True
    if not category_valid:
        needs_review = True
    if extraction_status in {"extract_error_metadata_only", "unsupported_metadata_only", "image_ocr_unavailable", "image_ocr_error", "image_ocr_no_text"} and not image_attached:
        needs_review = True

    parsed["confidence"] = _calibrate_confidence(
        conf,
        basis=basis,
        category_valid=category_valid,
        needs_review=needs_review,
        is_image=is_image,
        image_attached=image_attached,
        extracted_text=extracted_text,
        ocr_text=ocr_text,
        extraction_status=extraction_status,
        primary_category=str(parsed.get("primary_category") or ""),
        parse_status=str(parsed.get("status") or ""),
    )

    if parsed["confidence"] < 0.55:
        needs_review = True
    parsed["needs_human_review"] = needs_review
    parsed["category_valid"] = 1 if category_valid else 0
    parsed["original_category"] = original_category if original_category != parsed.get("primary_category") else ""

    # Keep short tag/list fields reasonable for CSV readability.
    parsed["secondary_tags"] = _stringify_value(parsed.get("secondary_tags"), max_items=3)
    for key in ("key_people", "key_organizations"):
        parsed[key] = _stringify_value(parsed.get(key))
    return parsed


def _image_payload(path: Path) -> tuple[str | None, str]:
    if not AI_SEND_IMAGES:
        return None, "EVIDENCE_AI_SEND_IMAGES is disabled."
    try:
        size = path.stat().st_size
        if size > IMAGE_MAX_BYTES:
            return None, f"Image is {size} bytes, above EVIDENCE_IMAGE_MAX_BYTES={IMAGE_MAX_BYTES}."
        return base64.b64encode(path.read_bytes()).decode("ascii"), ""
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _attach_image_to_messages(messages: list[dict[str, Any]], image_b64: str | None) -> None:
    if not image_b64:
        return
    # Ollama's /api/chat multimodal input expects base64 image strings on the
    # message object. Keep the image on the user message that contains the file prompt.
    for message in reversed(messages):
        if message.get("role") == "user":
            message["images"] = [image_b64]
            return


def categorize_file(
    *,
    path: str | Path,
    file_metadata: dict[str, Any],
    categories: str,
    context: str,
    model: str | None = None,
    max_chars: int = EXTRACT_MAX_CHARS,
) -> dict[str, Any]:
    p = Path(path)
    is_image = p.suffix.lower() in IMAGE_EXTS
    extraction = extract_text(p, max_chars=max_chars)
    extraction_text = str(extraction.get("text") or "")
    ocr_text = extraction_text if is_image else ""
    document_text = "" if is_image else extraction_text
    image_b64 = None
    image_attach_error = ""
    if is_image:
        image_b64, image_attach_error = _image_payload(p)

    image_attached = bool(image_b64)
    metadata = dict(file_metadata)
    metadata.update(
        {
            "is_image": is_image,
            "image_attached_to_ollama": image_attached,
            "image_attach_error": image_attach_error,
            "ocr_status": extraction.get("ocr_status") or extraction.get("status") or "",
        }
    )

    allowed_categories, category_definitions = _category_specs(categories)
    schema = _schema_for_categories(allowed_categories)
    messages = build_messages(
        file_metadata=metadata,
        extracted_text=document_text,
        ocr_text=ocr_text,
        extraction_status=str(extraction.get("status") or ""),
        extraction_error=str(extraction.get("error") or ""),
        categories=categories,
        context=context,
        allowed_categories=allowed_categories,
        category_definitions=category_definitions,
        is_image=is_image,
        image_attached=image_attached,
        image_attach_error=image_attach_error,
        extra_no_think_instruction=True,
    )
    _attach_image_to_messages(messages, image_b64)

    raw = _chat_with_fallbacks(messages, model=model, schema=schema)
    parsed = _parse_response(raw)

    original_category = parsed.get("primary_category") or ""
    canonical = _canonical_category(original_category, allowed_categories)
    category_valid = bool(canonical)
    retry_raw = ""
    if canonical:
        parsed["primary_category"] = canonical
        if canonical != original_category and parsed.get("status") == "ok":
            parsed["status"] = "category_normalized"
    else:
        retry_messages = _invalid_category_retry_messages(
            messages,
            invalid_category=original_category or "[blank]",
            allowed_categories=allowed_categories,
        )
        # Preserve any attached image on the retry prompt as well.
        _attach_image_to_messages(retry_messages, image_b64)
        retry_raw = _chat_with_fallbacks(retry_messages, model=model, schema=schema)
        retry_parsed = _parse_response(retry_raw)
        retry_category = retry_parsed.get("primary_category") or ""
        retry_canonical = _canonical_category(retry_category, allowed_categories)
        if retry_canonical:
            parsed = retry_parsed
            parsed["primary_category"] = retry_canonical
            category_valid = True
            parsed["status"] = "category_retry_ok"
        else:
            parsed["primary_category"] = "Unrelated or insufficient evidence"
            parsed["needs_human_review"] = True
            parsed["status"] = "invalid_category_fallback"
            category_valid = False

    parsed = _postprocess_fields(
        parsed,
        allowed_categories=allowed_categories,
        original_category=original_category,
        category_valid=category_valid,
        is_image=is_image,
        image_attached=image_attached,
        extracted_text=document_text,
        ocr_text=ocr_text,
        extraction_status=str(extraction.get("status") or ""),
    )
    parsed["category"] = parsed.get("primary_category")  # Backward-compatible alias for older app code.
    parsed["raw_response"] = raw + (("\n\n--- CATEGORY RETRY RAW ---\n" + retry_raw) if retry_raw else "")
    parsed["extraction_status"] = extraction.get("status")
    parsed["extraction_error"] = extraction.get("error")
    parsed["extracted_chars"] = extraction.get("chars", 0)
    parsed["image_sent"] = 1 if image_attached else 0
    parsed["ocr_status"] = extraction.get("ocr_status") or extraction.get("status") or ""
    return parsed
