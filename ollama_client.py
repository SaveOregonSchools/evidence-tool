"""Small Ollama /api/chat client using the same environment pattern as irs990-tool."""
from __future__ import annotations

import json
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


def _extract_content(data: dict[str, Any]) -> str:
    msg = data.get("message") or {}
    if isinstance(msg, dict) and msg.get("content"):
        return str(msg["content"]).strip()
    for key in ("response", "content", "text"):
        if data.get(key):
            return str(data[key]).strip()
    return ""


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
) -> str:
    """Call one of the configured Ollama endpoints and return assistant content.

    The retry loop is intentionally inside the client so a temporary Tailscale/VPN or
    Ollama hiccup does not immediately burn through an entire categorization batch.
    """
    selected_model = (model or OLLAMA_MODEL or "").strip()
    if not selected_model:
        raise ValueError("No Ollama model provided. Set OLLAMA_MODEL in .env or enter a model in the web form.")
    if not OLLAMA_ENDPOINTS:
        raise ValueError("No OLLAMA_ENDPOINTS configured.")

    payload: dict[str, Any] = {
        "model": selected_model,
        "messages": messages,
        "stream": False,
        "keep_alive": "30m",
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "num_ctx": int(num_ctx),
            "num_predict": int(num_predict),
        },
    }
    if response_format is not None:
        payload["format"] = response_format

    body = json.dumps(payload).encode("utf-8")
    errors: list[str] = []
    attempts = max(1, int(retries) + 1)

    for attempt in range(1, attempts + 1):
        for endpoint in OLLAMA_ENDPOINTS:
            req = urllib.request.Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(raw)
                    content = _extract_content(data)
                    if content:
                        return content
                    errors.append(f"attempt {attempt} {endpoint}: empty response; raw={raw[:500]}")
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    detail = str(e)
                errors.append(f"attempt {attempt} {endpoint}: HTTP {e.code}: {detail}")
            except urllib.error.URLError as e:
                errors.append(f"attempt {attempt} {endpoint}: {e}")
            except TimeoutError as e:
                errors.append(f"attempt {attempt} {endpoint}: timeout: {e}")
            except Exception as e:
                errors.append(f"attempt {attempt} {endpoint}: {type(e).__name__}: {e}")
        if attempt < attempts and retry_delay > 0:
            time.sleep(float(retry_delay))

    raise RuntimeError("Could not get a usable response from Ollama.\n" + "\n".join(errors[-10:]))
