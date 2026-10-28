"""Tests for the per-provider "list available models" helper (feature B).

The provider SDK clients are faked via monkeypatched constructors so the suite
never hits the network — we assert the ids we collect and the chat-model filtering
(OpenAI drops non-chat models; Gemini keeps only generateContent models), plus that
a provider failure surfaces as ``ModelListError``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from contextvault.llm.models import ModelListError, list_models


class _AsyncIter:
    """Async iterator over a fixed list — mimics an SDK's paginated list result."""

    def __init__(self, items: list[_Model]) -> None:
        self._it = iter(items)

    def __aiter__(self) -> _AsyncIter:
        return self

    async def __anext__(self) -> _Model:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Model:
    def __init__(self, id: str, supported_actions: list[str] | None = None) -> None:
        self.id = id
        self.name = id
        self.supported_actions = supported_actions or []


class _FakeModelsResource:
    def __init__(self, items: list[_Model]) -> None:
        self._items = items

    def list(self) -> _AsyncIter:  # OpenAI / Anthropic: sync call returns async iterable
        return _AsyncIter(self._items)


async def test_anthropic_lists_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_Model("claude-opus-4-8"), _Model("claude-sonnet-5")]

    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            self.models = _FakeModelsResource(items)

    monkeypatch.setattr("contextvault.llm.models.AsyncAnthropic", FakeAnthropic)
    models = await list_models("anthropic", api_key="sk-x")
    assert models == ["claude-opus-4-8", "claude-sonnet-5"]


async def test_openai_filters_to_chat_models(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _Model("gpt-4o"),
        _Model("o3-mini"),
        _Model("chatgpt-4o-latest"),
        _Model("text-embedding-3-small"),
        _Model("whisper-1"),
        _Model("dall-e-3"),
        _Model("tts-1"),
    ]

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.models = _FakeModelsResource(items)

    monkeypatch.setattr("contextvault.llm.models.AsyncOpenAI", FakeOpenAI)
    models = await list_models("openai", api_key="sk-x")
    assert models == ["chatgpt-4o-latest", "gpt-4o", "o3-mini"]


async def test_openrouter_returns_all_namespaced_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [_Model("openai/gpt-4o"), _Model("anthropic/claude-3.5-sonnet")]

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs.get("base_url")  # OpenRouter must pass a base_url
            self.models = _FakeModelsResource(items)

    monkeypatch.setattr("contextvault.llm.models.AsyncOpenAI", FakeOpenAI)
    models = await list_models(
        "openrouter", api_key="sk-x", base_url="https://openrouter.ai/api/v1"
    )
    assert models == ["anthropic/claude-3.5-sonnet", "openai/gpt-4o"]


async def test_custom_lists_all_models_via_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Model:
        def __init__(self, mid: str) -> None:
            self.id = mid

    class _FakeModels:
        def list(self) -> _FakeModels:
            return self

        def __aiter__(self) -> AsyncIterator[_Model]:
            async def gen() -> AsyncIterator[_Model]:
                for m in (_Model("llama3.1:8b"), _Model("nomic-embed-text")):
                    yield m

            return gen()

    class _FakeClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.models = _FakeModels()

    monkeypatch.setattr("contextvault.llm.models.AsyncOpenAI", _FakeClient)
    result = await list_models("custom", "sk-noauth", base_url="http://localhost:11434/v1")
    # No chat-family filter for custom: every id is returned (local names are arbitrary).
    assert result == ["llama3.1:8b", "nomic-embed-text"]
    assert captured["base_url"] == "http://localhost:11434/v1"


async def test_gemini_keeps_only_generate_content(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        _Model("models/gemini-2.5-flash", supported_actions=["generateContent", "countTokens"]),
        _Model("models/gemini-2.5-pro", supported_actions=["generateContent"]),
        _Model("models/text-embedding-004", supported_actions=["embedContent"]),
    ]

    class FakeAio:
        def __init__(self, items: list[_Model]) -> None:
            self.models = _FakeAioModels(items)

    class _FakeAioModels:
        def __init__(self, items: list[_Model]) -> None:
            self._items = items

        async def list(self) -> _AsyncIter:  # google-genai aio: awaitable → async iterable
            return _AsyncIter(self._items)

    class FakeGenaiClient:
        def __init__(self, **kwargs: object) -> None:
            self.aio = FakeAio(items)

    monkeypatch.setattr("contextvault.llm.models._genai_client", lambda api_key: FakeGenaiClient())
    models = await list_models("gemini", api_key="sk-x")
    assert models == ["gemini-2.5-flash", "gemini-2.5-pro"]


async def test_provider_error_becomes_model_list_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAnthropic:
        def __init__(self, **kwargs: object) -> None:
            raise RuntimeError("boom: bad key")

    monkeypatch.setattr("contextvault.llm.models.AsyncAnthropic", FakeAnthropic)
    with pytest.raises(ModelListError):
        await list_models("anthropic", api_key="bad")


async def test_unknown_provider_raises() -> None:
    with pytest.raises(ModelListError):
        await list_models("nope", api_key="x")
