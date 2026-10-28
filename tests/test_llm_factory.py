"""Tests for the ``get_llm_provider`` factory / default-provider selection.

The factory reads ``llm_provider`` and returns the matching provider, defaulting
to Gemini. ``genai.Client`` is stubbed so no API key is needed to construct one.
"""

import types
from typing import Any

import pytest

from contextvault.llm import LLMProvider, get_llm_provider
from contextvault.llm.anthropic import AnthropicLLMProvider
from contextvault.llm.gemini import GeminiLLMProvider
from contextvault.llm.openai import OpenAILLMProvider
from contextvault.llm.openrouter import OpenRouterLLMProvider


def _stub_gemini_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contextvault.llm.gemini.genai.Client", lambda **kwargs: object())


def _stub_openai_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contextvault.llm.openai.AsyncOpenAI", lambda **kwargs: object())


def _stub_openrouter_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contextvault.llm.openrouter.AsyncOpenAI", lambda **kwargs: object())


def _stub_anthropic_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contextvault.llm.anthropic.AsyncAnthropic", lambda **kwargs: object())


def test_default_provider_is_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gemini_client(monkeypatch)
    monkeypatch.setattr(
        "contextvault.llm.get_settings",
        lambda: types.SimpleNamespace(llm_provider="gemini"),
    )
    provider = get_llm_provider()
    assert isinstance(provider, GeminiLLMProvider)
    assert isinstance(provider, LLMProvider)


def test_explicit_name_overrides_configured_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gemini_client(monkeypatch)
    # Case-insensitive; explicit name wins regardless of the setting.
    provider: Any = get_llm_provider("GEMINI")
    assert isinstance(provider, GeminiLLMProvider)


def test_openai_provider_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_openai_client(monkeypatch)
    # Case-insensitive selection, matching the other providers.
    provider: Any = get_llm_provider("OpenAI")
    assert isinstance(provider, OpenAILLMProvider)
    assert isinstance(provider, LLMProvider)


def test_openrouter_provider_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_openrouter_client(monkeypatch)
    # Case-insensitive selection, matching the other providers.
    provider: Any = get_llm_provider("OpenRouter")
    assert isinstance(provider, OpenRouterLLMProvider)
    assert isinstance(provider, LLMProvider)


def test_anthropic_provider_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_anthropic_client(monkeypatch)
    # Anthropic is a valid stored provider; per-repo routing
    # makes it constructible through the factory like the others.
    provider: Any = get_llm_provider("Anthropic")
    assert isinstance(provider, AnthropicLLMProvider)
    assert isinstance(provider, LLMProvider)


def test_supplied_model_and_key_thread_through_to_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    # Per-repo routing passes each repository's model and decrypted key
    # into the factory; they must reach the constructed provider rather than be
    # dropped in favour of the process-wide settings defaults.
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "contextvault.llm.openai.AsyncOpenAI",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    provider: Any = get_llm_provider("openai", api_key="sk-repo-key", model="gpt-4o-mini")
    assert provider._model == "gpt-4o-mini"
    assert captured["api_key"] == "sk-repo-key"


def test_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="not-yet-wired"):
        get_llm_provider("does-not-exist")


def test_custom_provider_selectable_with_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "contextvault.llm.openai.AsyncOpenAI",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    provider: Any = get_llm_provider(
        "custom", api_key="sk-noauth", model="llama3.1:8b", base_url="http://localhost:11434/v1"
    )
    assert isinstance(provider, OpenAILLMProvider)
    assert provider._model == "llama3.1:8b"
    assert captured["base_url"] == "http://localhost:11434/v1"
