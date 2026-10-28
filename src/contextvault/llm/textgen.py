"""Plain-text generation via a provider's global key — non-RAG LLM calls.

The RAG loop speaks through ``LLMProvider.answer`` (grounded, cited); some
features — report SQL generation — need a raw completion instead. This module
mirrors :mod:`contextvault.llm.ocr`: standalone per-provider async functions
dispatched by name, every failure wrapped in :class:`TextGenError`.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from openai import AsyncOpenAI

from contextvault.core.config import get_settings

if TYPE_CHECKING:
    from google import genai

__all__ = ["TextGenError", "generate_text", "is_transient", "retry_after"]

_MAX_TOKENS = 2048

# One report is many calls — a plan, then up to three SQL attempts per section,
# then prose. At those odds a provider's routine "try again later" was reaching
# the orchestrator, which has no retry of its own for anything but a *contract*
# violation, so a momentary 503 marked the whole report FAILED — a terminal state
# no route recovers from. Transport hiccups are retried here, where every caller
# benefits and where the failure is still identifiable.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF = (1.0, 3.0)
# A rate-limited provider says how long to wait, and it is the only party that
# knows: a free-tier Gemini key allows five requests a minute and answers a 429
# with ``retryDelay: '39s'``. Backing off one second against that just spends the
# retry budget without ever clearing the window. Capped so a provider cannot
# park a background task indefinitely.
_MAX_SERVER_DELAY = 45.0
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)
_TRANSIENT = re.compile(
    r"\b(429|500|502|503|504)\b"
    r"|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|INTERNAL"
    r"|overloaded|rate.?limit|too many requests|timed? ?out|timeout"
    r"|temporarily|try again|connection (reset|aborted|error)",
    re.IGNORECASE,
)
# Checked first: "invalid api key" must never be retried, however it is worded,
# and some providers put a 4xx code in the same string as the word "error".
_PERMANENT = re.compile(
    r"\b(400|401|403|404)\b"
    r"|PERMISSION_DENIED|UNAUTHENTICATED|INVALID_ARGUMENT|NOT_FOUND"
    r"|invalid.{0,12}(key|token|model|request)|unsupported",
    re.IGNORECASE,
)


class TextGenError(Exception):
    """The provider could not generate text (bad key, network, quota, …)."""


async def _sleep(seconds: float) -> None:
    """The backoff pause, as a seam so tests need not actually wait."""
    await asyncio.sleep(seconds)


def retry_after(exc: BaseException, fallback: float) -> float:
    """How long to wait before retrying: the provider's own answer, or ``fallback``."""
    match = _RETRY_DELAY.search(f"{exc}")
    if match is None:
        return fallback
    return min(max(float(match[1]), fallback), _MAX_SERVER_DELAY)


def is_transient(exc: BaseException) -> bool:
    """Whether retrying this failure could plausibly succeed.

    Deliberately conservative: an unrecognised failure is treated as permanent,
    so a genuinely broken configuration fails fast instead of taking four seconds
    to do it, once per call, across every section of a report.
    """
    text = f"{type(exc).__name__}: {exc}"
    if _PERMANENT.search(text):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return bool(_TRANSIENT.search(text))


def _genai_client(api_key: str) -> genai.Client:
    from google import genai

    return genai.Client(api_key=api_key)


async def _generate_gemini(api_key: str, model: str, prompt: str) -> str:
    from google.genai import types

    client = _genai_client(api_key)
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(max_output_tokens=_MAX_TOKENS),
    )
    return (response.text or "").strip()


async def _generate_openai_compatible(
    api_key: str, model: str, prompt: str, base_url: str | None
) -> str:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    completion = await client.chat.completions.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return (completion.choices[0].message.content or "").strip()


async def _generate_anthropic(api_key: str, model: str, prompt: str) -> str:
    client = AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model=model,
        max_tokens=_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()


async def _dispatch(
    name: str, api_key: str, model: str, prompt: str, base_url: str | None
) -> str | None:
    if name == "gemini":
        return await _generate_gemini(api_key, model, prompt)
    if name == "openai":
        return await _generate_openai_compatible(api_key, model, prompt, None)
    if name == "openrouter":
        base = base_url or get_settings().openrouter_base_url
        return await _generate_openai_compatible(api_key, model, prompt, base)
    if name == "custom":
        return await _generate_openai_compatible(api_key, model, prompt, base_url)
    if name == "anthropic":
        return await _generate_anthropic(api_key, model, prompt)
    return None


async def generate_text(
    provider: str, api_key: str, model: str, *, prompt: str, base_url: str | None = None
) -> str:
    """Generate a completion for ``prompt`` with ``provider``'s model ``model``.

    Transient provider failures are retried with a short backoff; everything else
    fails on the first attempt (see :func:`is_transient`).
    """
    name = provider.lower()
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            answer = await _dispatch(name, api_key, model, prompt, base_url)
        except TextGenError:
            raise
        except Exception as exc:  # noqa: BLE001 — any SDK/network failure becomes a clean error
            if attempt + 1 < _RETRY_ATTEMPTS and is_transient(exc):
                await _sleep(retry_after(exc, _RETRY_BACKOFF[attempt]))
                continue
            raise TextGenError(f"Could not generate text: {exc}") from exc
        if answer is None:
            raise TextGenError(f"Unsupported provider: {provider!r}")
        return answer
    raise TextGenError(f"Could not generate text with {provider!r} after {_RETRY_ATTEMPTS} tries.")
