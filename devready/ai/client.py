"""A small reusable OpenRouter chat client that returns parsed JSON.

The README parser has its own (more elaborate) model-fallback flow tuned for
extraction. This module exposes a *general* ``ask_llm_json`` used by features
that just need "send a prompt, get back a JSON object" — currently the
self-healing install loop (:mod:`devready.ai.healer`).

It reuses the same endpoint, curated free-model list, and JSON-extraction helper
as the README parser, so behaviour (and the self-healing across free models)
stays consistent. Returns ``None`` on any failure — callers degrade gracefully.
"""

from __future__ import annotations

from typing import List, Optional

from ..config import Config
from ..utils import console
from .readme_parser import (
    FALLBACK_MODELS,
    MAX_MODEL_ATTEMPTS,
    OPENROUTER_URL,
    _extract_json,
)


def ask_llm_json(
    config: Config,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.1,
) -> Optional[dict]:
    """Send a single chat request and return the parsed JSON object, or None.

    Tries the user's configured model first, then the curated free fallbacks, so
    a retired or rate-limited model doesn't break the feature. Any network error,
    bad key (401), or unparseable reply yields ``None`` — the caller is expected
    to carry on without the LLM's help rather than fail.
    """
    if not config.llm.is_configured:
        return None
    try:
        import httpx
    except ImportError:
        return None

    headers = {
        "Authorization": f"Bearer {config.llm.api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/ahmadkassem511/DevReady",
        "X-Title": "DevReady",
    }
    models: List[str] = [config.llm.model] + [m for m in FALLBACK_MODELS if m != config.llm.model]

    tried = 0
    for model in models:
        if tried >= MAX_MODEL_ATTEMPTS:
            break
        tried += 1
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        try:
            response = httpx.post(OPENROUTER_URL, json=payload, headers=headers, timeout=60)
        except Exception:
            return None  # network error — not fixable by trying another model

        if response.status_code == 200:
            try:
                content = response.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError):
                continue
            data = _extract_json(content)
            if data is not None:
                return data
            continue  # unparseable — try the next model
        if response.status_code == 401:
            return None  # bad key — trying another model won't help
        # 404 / 429 / other — fall through to the next model.

    return None
