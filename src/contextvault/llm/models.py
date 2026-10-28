"""List the models a provider currently offers, for the admin model picker (feature B).

The per-repository LLM config lets an admin pick a model; rather than typing an id by
hand, this module fetches the live catalogue from the provider's own API using the
admin-supplied (or stored) key. Each provider's SDK exposes a "list models" call; we
collect the ids and apply light chat-model filtering so the dropdown isn't polluted with
embedding / audio / image models.

Any provider, auth, or network failure is wrapped in :class:`ModelListError` so the API
layer can return a single clean 400 instead of leaking SDK-specific exceptions.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from google import genai

__all__ = ["ModelListError", "list_models"]


class ModelListError(Exception):
    """A provider's model list could not be fetched (bad key, network, etc.)."""


# OpenAI's catalogue mixes chat models with embeddings, audio (whisper/tts), and image
# (dall-e) models. Keep the chat families: ``gpt-*``, the ``o<n>`` reasoning line, and
# the ``chatgpt-*`` snapshots.
_OPENAI_CHAT = re.compile(r"^(gpt-|chatgpt|o\d)")


def _is_openai_chat_id(model_id: str) -> bool:
    return bool(_OPENAI_CHAT.match(model_id))


def _genai_client(api_key: str) -> genai.Client:
    """Build a Google GenAI client (lazy import; monkeypatched in tests)."""
    from google import genai

    return genai.Client(api_key=api_key)


async def _list_anthropic(api_key: str) -> list[str]:
    client = AsyncAnthropic(api_key=api_key)
    return sorted({m.id async for m in client.models.list()})


async def _list_openai(api_key: str) -> list[str]:
    client = AsyncOpenAI(api_key=api_key)
    return sorted({m.id async for m in client.models.list() if _is_openai_chat_id(m.id)})


async def _list_openai_compatible(api_key: str, base_url: str | None) -> list[str]:
    # OpenAI-compatible endpoints (OpenRouter, self-hosted) already expose chat model
    # ids directly; return them all rather than applying the OpenAI-family filter.
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return sorted({m.id async for m in client.models.list()})


async def _list_gemini(api_key: str) -> list[str]:
    client = _genai_client(api_key)
    pager = await client.aio.models.list()
    return sorted(
        {
            (m.name or "").removeprefix("models/")
            async for m in pager
            if m.name and "generateContent" in (getattr(m, "supported_actions", None) or [])
        }
    )


async def list_models(provider: str, api_key: str, *, base_url: str | None = None) -> list[str]:
    """Return the chat models ``provider`` currently offers for ``api_key``.

    ``base_url`` is used for OpenRouter and custom OpenAI-compatible endpoints. Raises
    :class:`ModelListError` for an unknown provider or any provider-side failure.
    """
    name = provider.lower()
    try:
        if name == "anthropic":
            return await _list_anthropic(api_key)
        if name == "openai":
            return await _list_openai(api_key)
        if name == "openrouter":
            return await _list_openai_compatible(api_key, base_url)
        if name == "gemini":
            return await _list_gemini(api_key)
        if name == "custom":
            return await _list_openai_compatible(api_key, base_url)
    except ModelListError:
        raise
    except Exception as exc:  # noqa: BLE001 — any SDK failure becomes a clean 400
        raise ModelListError(f"Could not list models: {exc}") from exc
    raise ModelListError(f"Unsupported provider: {provider!r}")
