"""Small Ollama /api/chat client using the same environment pattern as irs990-tool."""
from __future__ import annotations

import copy
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from common import (
    OLLAMA_ENDPOINTS,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_NUM_PREDICT,
    OLLAMA_RETRIES,
    OLLAMA_RETRY_DELAY,
    OLLAMA_TIMEOUT,
)

PATCH_ID = "2026-06-12-ollama-thinking-v3"


class OllamaClientError(RuntimeError):
    """Base class for local Ollama client errors."""


class OllamaEmptyResponseError(OllamaClientError):
    """Raised when Ollama returns JSON but no assistant content."""

    def __init__(self, message: str, *, had_thinking: bool = False) -> None:
        super().__init__(message)
        self.had_thinking = had_thinking


def _parse_think_env(value: str | None) -> bool | str | None:
    """Parse OLLAMA_THINK from .env.

    The evidence tool defaults this to False because batch categorization needs a
    short final JSON answer. Thinking traces are useful for interactive analysis,
    but they can make Ollama responses appear empty because final text belongs in
    message.content while reasoning belongs in message.thinking.
    """
    if value is None or str(value).strip() == "":
        return False
    normalized = str(value).strip().lower()
    if normalized in {"auto", "none", "default", "omit"}:
        return None
    if normalized in {"false", "0", "no", "n", "off", "disable", "disabled", "nothink", "no_think"}:
        return False
    if normalized in {"true", "1", "yes", "y", "on", "enable", "enabled", "think"}:
        return True
    if normalized in {"low", "medium", "high"}:
        return normalized
    return False


def _normalize_endpoint(endpoint: str) -> str:
    """Allow users to enter either a base Ollama URL or a full /api/chat URL."""
    ep = (endpoint or "").strip().rstrip("/")
    if not ep:
        return ep
    if ep.endswith("/api/chat"):
        return ep
    if ep.endswith("/api/generate"):
        return ep[: -len("/api/generate")] + "/api/chat"
    return ep + "/api/chat"


OLLAMA_THINK = _parse_think_env(os.getenv("OLLAMA_THINK", "false"))
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m").strip() or "30m"
OLLAMA_TOP_P = float(os.getenv("OLLAMA_TOP_P", "0.9"))
OLLAMA_REPEAT_PENALTY = float(os.getenv("OLLAMA_REPEAT_PENALTY", "1.05"))
OLLAMA_ERROR_RAW_CHARS = int(os.getenv("OLLAMA_ERROR_RAW_CHARS", "2500"))
NORMALIZED_OLLAMA_ENDPOINTS = [_normalize_endpoint(ep) for ep in OLLAMA_ENDPOINTS if _normalize_endpoint(ep)]


def _extract_content(data: dict[str, Any]) -> str:
    """Return final assistant content from common Ollama response shapes.

    Do not use message.thinking as a successful answer. Ollama uses that field
    for the reasoning trace; message.content is where the actual final answer is
    supposed to appear.
    """
    msg = data.get("message") or {}
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    for key in ("response", "content", "text"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_thinking(data: dict[str, Any]) -> str:
    msg = data.get("message") or {}
    if isinstance(msg, dict):
        thinking = msg.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            return thinking.strip()
    thinking = data.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip()
    return ""


def _response_summary_for_log(data: dict[str, Any], raw: str, *, think: bool | str | None) -> tuple[str, bool]:
    """Return a compact diagnostic summary and whether a thinking field was present."""
    thinking = _extract_thinking(data)
    msg = data.get("message") or {}
    had_thinking = bool(thinking)
    parts: list[str] = []
    if had_thinking:
        parts.append(f"message.thinking present ({len(thinking)} chars) but final assistant content is empty")
        parts.append(f"request_think={think!r}")
    if isinstance(msg, dict) and msg.get("content") == "":
        parts.append("message.content is an empty string")
    done_reason = data.get("done_reason") or (msg.get("done_reason") if isinstance(msg, dict) else None)
    if done_reason:
        parts.append(f"done_reason={done_reason}")
    for key in ("total_duration", "load_duration", "prompt_eval_count", "eval_count"):
        if key in data:
            parts.append(f"{key}={data.get(key)}")
    if had_thinking:
        parts.append(
            "This usually means the running Ollama/model stack is still producing thinking tokens. "
            "Confirm this file is loaded with /debug/version, update Ollama if needed, or test a non-thinking instruct model."
        )
    if not parts:
        parts.append(raw[: max(0, OLLAMA_ERROR_RAW_CHARS)])
    return "; ".join(parts), had_thinking


def _post_json(endpoint: str, payload: dict[str, Any], timeout: int) -> tuple[str, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Ollama returned non-object JSON: {type(data).__name__}")
        return raw, data


def _payload_variants(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return request variants from best to fallback.

    Top-level think:false is the intended request. The fallback omits the field so
    an older or unusual Ollama server gets one chance to respond instead of
    failing on an unrecognized field. This fallback is not expected to help Qwen
    thinking behavior, but it helps diagnose server compatibility.
    """
    variants: list[tuple[str, dict[str, Any]]] = [("configured", payload)]
    if "think" in payload:
        without_think = copy.deepcopy(payload)
        without_think.pop("think", None)
        variants.append(("without_think_field", without_think))
    return variants


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.1,
    num_ctx: int = OLLAMA_NUM_CTX,
    num_predict: int = OLLAMA_NUM_PREDICT,
    timeout: int = OLLAMA_TIMEOUT,
    response_format: str | dict[str, Any] | None = None,
    retries: int = OLLAMA_RETRIES,
    retry_delay: float = OLLAMA_RETRY_DELAY,
    think: bool | str | None = OLLAMA_THINK,
) -> str:
    """Call one configured Ollama endpoint and return assistant content."""
    selected_model = (model or OLLAMA_MODEL or "").strip()
    if not selected_model:
        raise ValueError("No Ollama model provided. Set OLLAMA_MODEL in .env or enter a model in the web form.")
    if not NORMALIZED_OLLAMA_ENDPOINTS:
        raise ValueError("No OLLAMA_ENDPOINTS configured.")

    payload: dict[str, Any] = {
        "model": selected_model,
        "messages": messages,
        "stream": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "temperature": float(temperature),
            "top_p": OLLAMA_TOP_P,
            "repeat_penalty": OLLAMA_REPEAT_PENALTY,
            "num_ctx": int(num_ctx),
            "num_predict": int(num_predict),
        },
    }
    # Important: think belongs at the top level of the Ollama request.
    if think is not None:
        payload["think"] = think
    if response_format is not None:
        payload["format"] = response_format

    errors: list[str] = []
    any_thinking_empty = False
    attempts = max(1, int(retries) + 1)

    for attempt in range(1, attempts + 1):
        for endpoint in NORMALIZED_OLLAMA_ENDPOINTS:
            for variant_name, variant_payload in _payload_variants(payload):
                variant_think = variant_payload.get("think", None)
                try:
                    raw, data = _post_json(endpoint, variant_payload, timeout)
                    content = _extract_content(data)
                    if content:
                        return content
                    summary, had_thinking = _response_summary_for_log(data, raw, think=variant_think)
                    any_thinking_empty = any_thinking_empty or had_thinking
                    errors.append(f"attempt {attempt} {endpoint} [{variant_name}]: empty assistant content; {summary}")
                except urllib.error.HTTPError as e:
                    try:
                        detail = e.read().decode("utf-8", errors="replace")[:OLLAMA_ERROR_RAW_CHARS]
                    except Exception:
                        detail = str(e)
                    errors.append(f"attempt {attempt} {endpoint} [{variant_name}]: HTTP {e.code}: {detail}")
                except urllib.error.URLError as e:
                    errors.append(f"attempt {attempt} {endpoint} [{variant_name}]: {e}")
                except TimeoutError as e:
                    errors.append(f"attempt {attempt} {endpoint} [{variant_name}]: timeout: {e}")
                except json.JSONDecodeError as e:
                    errors.append(f"attempt {attempt} {endpoint} [{variant_name}]: invalid JSON from Ollama: {e}")
                except Exception as e:
                    errors.append(f"attempt {attempt} {endpoint} [{variant_name}]: {type(e).__name__}: {e}")
        if attempt < attempts and retry_delay > 0:
            time.sleep(float(retry_delay))

    message = "Could not get a usable response from Ollama.\n" + "\n".join(errors[-12:])
    if any_thinking_empty:
        raise OllamaEmptyResponseError(message, had_thinking=True)
    raise OllamaClientError(message)
