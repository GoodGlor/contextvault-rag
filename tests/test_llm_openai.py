"""Tests for the OpenAI (ChatGPT) ``LLMProvider``.

The OpenAI SDK is never called for real: a fake stands in for ``AsyncOpenAI``,
capturing the request the provider builds (``model``, ``messages``,
``max_tokens``) and returning a canned completion. That pins the same behaviours
the Anthropic and Gemini providers guarantee — numbered-chunk grounding, answer
only from the sources, honest "not in this vault" short-circuit, and ``[n]``
markers mapped back to source chunks — without a network call or an API key.
"""

import uuid
from typing import Any, cast

from openai import AsyncOpenAI

from contextvault.llm import LLMProvider
from contextvault.llm.openai import OpenAILLMProvider
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


def _provider(client: _FakeClient, **kwargs: Any) -> OpenAILLMProvider:
    return OpenAILLMProvider(client=cast(AsyncOpenAI, client), **kwargs)


def test_provider_satisfies_protocol() -> None:
    provider: LLMProvider = _provider(_FakeClient())
    assert isinstance(provider, LLMProvider)


async def test_empty_chunks_short_circuits_without_calling_api() -> None:
    client = _FakeClient()
    result = await _provider(client).answer("anything?", [])

    assert result.text
    assert result.citations == []
    assert result.not_in_vault is True  # the outcome is a flagged refusal
    assert client.chat.completions.calls == []  # the model is never consulted


async def test_grounded_cited_answer_is_not_flagged() -> None:
    client = _FakeClient(reply="Grounded. [1]")
    result = await _provider(client).answer("q", [_chunk(0)])

    assert result.citations
    assert result.not_in_vault is False


async def test_answer_returns_model_text() -> None:
    client = _FakeClient(reply="The deploy step runs on push. [1]")
    result = await _provider(client).answer("how does deploy work?", [_chunk(0)])

    assert result.text == "The deploy step runs on push. [1]"


async def test_model_is_configurable() -> None:
    client = _FakeClient()
    await _provider(client, model="gpt-4o-mini").answer("q", [_chunk(0)])

    assert client.chat.completions.calls[0]["model"] == "gpt-4o-mini"


async def test_default_model_is_openai() -> None:
    client = _FakeClient()
    await _provider(client).answer("q", [_chunk(0)])

    assert client.chat.completions.calls[0]["model"] == "gpt-4o"


async def test_prompt_numbers_chunks_and_grounds_the_model() -> None:
    client = _FakeClient()
    chunks = [_chunk(0), _chunk(1)]
    await _provider(client).answer("what is x?", chunks)

    messages = client.chat.completions.calls[0]["messages"]
    system = next(m["content"] for m in messages if m["role"] == "system").lower()
    assert "only" in system
    assert "not in this vault" in system

    user = next(m["content"] for m in messages if m["role"] == "user")
    assert "[1]" in user and "passage 0" in user
    assert "[2]" in user and "passage 1" in user
    assert "what is x?" in user


async def test_max_tokens_passed_in_request() -> None:
    client = _FakeClient()
    await _provider(client, max_tokens=321).answer("q", [_chunk(0)])

    assert client.chat.completions.calls[0]["max_tokens"] == 321


async def test_citation_markers_map_back_to_chunks() -> None:
    client = _FakeClient(reply="A [1]. B [2].")
    chunks = [_chunk(0), _chunk(1)]
    result = await _provider(client).answer("q", chunks)

    assert [c.number for c in result.citations] == [1, 2]
    assert [(c.chunk_id, c.source_id, c.char_start, c.char_end) for c in result.citations] == [
        (chunks[0].chunk_id, chunks[0].source_id, chunks[0].char_start, chunks[0].char_end),
        (chunks[1].chunk_id, chunks[1].source_id, chunks[1].char_start, chunks[1].char_end),
    ]


async def test_repeated_marker_yields_one_citation_in_first_use_order() -> None:
    client = _FakeClient(reply="B [2]. A [1]. Again [2].")
    chunks = [_chunk(0), _chunk(1)]
    result = await _provider(client).answer("q", chunks)

    assert [c.number for c in result.citations] == [2, 1]
    assert [c.chunk_id for c in result.citations] == [chunks[1].chunk_id, chunks[0].chunk_id]


async def test_out_of_range_markers_are_ignored() -> None:
    client = _FakeClient(reply="Fabricated [5] and real [1].")
    result = await _provider(client).answer("q", [_chunk(0)])

    assert [c.number for c in result.citations] == [1]


async def test_blank_response_content_yields_empty_answer() -> None:
    """OpenAI's ``message.content`` can be None (e.g. a refusal/filtered reply).

    Grounding nothing, it is flagged ``not_in_vault`` rather than passed off as a
    grounded answer.
    """
    client = _FakeClient(reply=None)
    result = await _provider(client).answer("q", [_chunk(0)])

    assert result.text == ""
    assert result.citations == []
    assert result.not_in_vault is True


def test_client_receives_base_url(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def _fake_async_openai(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("contextvault.llm.openai.AsyncOpenAI", _fake_async_openai)
    OpenAILLMProvider(api_key="sk-noauth", base_url="http://localhost:11434/v1")

    assert captured["base_url"] == "http://localhost:11434/v1"
    assert captured["api_key"] == "sk-noauth"
