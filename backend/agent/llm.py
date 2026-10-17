"""Thin adapter for any OpenAI-compatible chat endpoint (Groq / Gemini / Ollama). Optional.

Any failure (no key, timeout, 429, bad output) returns None and the caller falls back to the
deterministic template. The fallback is recorded in the report so the UI can show
"LLM unavailable -> deterministic".
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import httpx

from ..config import settings

SYSTEM = (
    "You are TraceLens, an incident analysis assistant. Write a concise (max 6 sentences) plain-English narrative "
    "from the EVIDENCE JSON only. Rules: mention ONLY services and IDs that appear in the evidence; never invent entities; "
    "never say 'proven' or 'proves' — say 'best-supported explanation'; label items as correlated, supported, ruled out or inconclusive, "
    "using EXACTLY the status each hypothesis has in the evidence (an 'inconclusive' hypothesis is never 'supported'); if the outcome is "
    "'inconclusive', say so and list the missing data. Do not mention hypothesis IDs (inv-…-hN); refer to deployments, services and incidents instead. "
    "State that confidence is the share of graph checks passed, not a probability."
)


class LLMUnavailable(Exception):
    pass


def available() -> bool:
    return settings.llm_configured


def write_narrative(evidence: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Returns (narrative, error). narrative is None when the LLM could not be used."""
    if not settings.llm_configured:
        return None, "LLM not configured (LLM_BASE_URL / LLM_API_KEY / LLM_MODEL)"
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": settings.llm_model,
        "temperature": 0.2,
        # generous: reasoning models (e.g. gemini-2.5-flash) spend hidden "thinking" tokens from this budget
        "max_tokens": 2000,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "EVIDENCE:\n" + json.dumps(evidence, default=str)[:12000]},
        ],
    }
    try:
        with httpx.Client(timeout=settings.llm_timeout_s) as client:
            resp = client.post(url, json=body, headers={"Authorization": f"Bearer {settings.llm_api_key}"})
            if resp.status_code == 503:  # bounded retry: exactly one, for transient "high demand" errors
                time.sleep(1.5)
                resp = client.post(url, json=body, headers={"Authorization": f"Bearer {settings.llm_api_key}"})
        if resp.status_code == 429:
            return None, "LLM rate limited (429)"
        if resp.status_code >= 400:
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")[:160]
            except Exception:  # noqa: BLE001
                detail = resp.text[:160]
            return None, f"LLM HTTP {resp.status_code}: {detail or resp.reason_phrase}"
        data = resp.json()
        choice = data["choices"][0]
        text = (choice["message"].get("content") or "").strip()
        if not text:
            return None, "LLM returned empty output"
        if choice.get("finish_reason") == "length":
            return None, "LLM output truncated (finish_reason=length)"
        return text, None
    except httpx.TimeoutException:
        return None, f"LLM timeout after {settings.llm_timeout_s}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"LLM error: {type(exc).__name__}: {str(exc)[:120]}"
