"""Tests for the OpenRouter ``LLMProvider``.

OpenRouter speaks the OpenAI Chat Completions wire format, so this provider is a
thin reuse of the OpenAI one — same request shape, same numbered-chunk grounding
and ``[n]`` citation mapping — pointed at OpenRouter's base URL with its own key
and (namespaced) model id. These tests pin two things: that the OpenRouter-specific
wiring is correct (base URL, key, default model) and that the behaviour inherited
from the OpenAI provider still holds. No network call and no API key are needed —
a fake stands in for ``AsyncOpenAI``.
"""

import uuid
from typing import Any, cast

from openai import AsyncOpenAI

from contextvault.llm import LLMProvider
from contextvault.llm.openai import OpenAILLMProvider
from contextvault.llm.openrouter import OpenRouterLLMProvider
from contextvault.retrieval import RetrievedChunk


def _chunk(ordinal: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        ordinal=ordinal,
        content=f"passage {ordinal}",
        char_start=ordinal * 100,
        char_end=ordinal * 100 + 50,
        score=0.9,
    )


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeCompletion:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, reply: str | None) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, model: str, messages: Any, **kwargs: Any) -> _FakeCompletion:
        self.calls.append({"model": model, "messages": messages, **kwargs})
        return _FakeCompletion(self._reply)


class _FakeChat:
    def __init__(self, reply: str | None) -> None:
        self.completions = _FakeCompletions(reply)


class _FakeClient:
    """Structural stand-in for ``AsyncOpenAI`` — only ``.chat.completions.create``."""

    def __init__(self, reply: str | None = "grounded answer [1]") -> None:
        self.chat = _FakeChat(reply)


def _provider(client: _FakeClient, **kwargs: Any) -> OpenRouterLLMProvider:
    return OpenRouterLLMProvider(client=cast(AsyncOpenAI, client), **kwargs)


def test_provider_satisfies_protocol() -> None:
    provider: LLMProvider = _provider(_FakeClient())
    assert isinstance(provider, LLMProvider)


def test_reuses_the_openai_provider() -> None:
    # The card asks us to reuse the OpenAI implementation; subclassing keeps the
    # request shape and answer machinery identical, so behaviour can't drift.
    assert issubclass(OpenRouterLLMProvider, OpenAILLMProvider)


async def test_empty_chunks_short_circuits_without_calling_api() -> None:
    client = _FakeClient()
    result = await _provider(client).answer("anything?", [])

    assert result.text
    assert result.citations == []
    assert result.not_in_vault is True
    assert client.chat.completions.calls == []  # the model is never consulted


async def test_grounded_cited_answer_is_not_flagged() -> None:
    client = _FakeClient(reply="Grounded. [1]")
    result = await _provider(client).answer("q", [_chunk(0)])

    assert result.citations
    assert result.not_in_vault is False


async def test_prompt_grounds_the_model_in_numbered_chunks() -> None:
    client = _FakeClient()
    await _provider(client).answer("what is x?", [_chunk(0), _chunk(1)])

    messages = client.chat.completions.calls[0]["messages"]
    system = next(m["content"] for m in messages if m["role"] == "system").lower()
    assert "only" in system
    assert "not in this vault" in system

    user = next(m["content"] for m in messages if m["role"] == "user")
    assert "[1]" in user and "passage 0" in user
    assert "[2]" in user and "passage 1" in user
    assert "what is x?" in user


async def test_citation_markers_map_back_to_chunks() -> None:
    client = _FakeClient(reply="A [1]. B [2].")
    chunks = [_chunk(0), _chunk(1)]
    result = await _provider(client).answer("q", chunks)

    assert [c.number for c in result.citations] == [1, 2]
    assert [(c.chunk_id, c.source_id) for c in result.citations] == [
        (chunks[0].chunk_id, chunks[0].source_id),
        (chunks[1].chunk_id, chunks[1].source_id),
    ]


async def test_default_model_is_openrouter_namespaced() -> None:
    client = _FakeClient()
    await _provider(client).answer("q", [_chunk(0)])

    # OpenRouter model ids are namespaced by vendor; the default reflects that.
    assert client.chat.completions.calls[0]["model"] == "openai/gpt-4o"


async def test_model_is_configurable() -> None:
    client = _FakeClient()
    await _provider(client, model="anthropic/claude-3.5-sonnet").answer("q", [_chunk(0)])

    assert client.chat.completions.calls[0]["model"] == "anthropic/claude-3.5-sonnet"


async def test_max_tokens_passed_in_request() -> None:
    client = _FakeClient()
    await _provider(client, max_tokens=321).answer("q", [_chunk(0)])

    assert client.chat.completions.calls[0]["max_tokens"] == 321


def test_default_client_points_at_openrouter(monkeypatch: Any) -> None:
    # No injected client: the provider must build an AsyncOpenAI aimed at
    # OpenRouter's base URL, authenticated with the OpenRouter key.
    captured: dict[str, Any] = {}

    def _fake_async_openai(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("contextvault.llm.openrouter.AsyncOpenAI", _fake_async_openai)
    OpenRouterLLMProvider(api_key="or-secret")

    assert captured["api_key"] == "or-secret"
    assert "openrouter.ai" in captured["base_url"]
