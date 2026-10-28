"""Tests for the Anthropic (Claude) ``LLMProvider``.

The Anthropic SDK is never called for real here: a small fake stands in for
``AsyncAnthropic``, capturing the request the provider builds and returning a
canned message. That lets us pin the grounding behaviour (numbered chunks in the
prompt, answer only from them), the configurable model, the honest "not in this
vault" short-circuit, and the mapping of ``[n]`` markers back to source chunks —
all without a network call or an API key.
"""

import uuid
from typing import Any, cast

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

from contextvault.llm import LLMProvider
from contextvault.llm.anthropic import AnthropicLLMProvider
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


class _Message:
    def __init__(self, blocks: list[Any]) -> None:
        self.content = blocks


class _FakeMessages:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Message:
        self.calls.append(kwargs)
        return _Message([TextBlock(type="text", text=self._reply, citations=None)])


class _FakeClient:
    """Structural stand-in for ``AsyncAnthropic`` — only ``.messages.create``."""

    def __init__(self, reply: str = "grounded answer [1]") -> None:
        self.messages = _FakeMessages(reply)


def _provider(client: _FakeClient, **kwargs: Any) -> AnthropicLLMProvider:
    return AnthropicLLMProvider(client=cast(AsyncAnthropic, client), **kwargs)


def test_provider_satisfies_protocol() -> None:
    provider: LLMProvider = _provider(_FakeClient())
    assert isinstance(provider, LLMProvider)


async def test_empty_chunks_short_circuits_without_calling_api() -> None:
    """No relevant chunks → honest 'not in this vault', no API call, no citations."""
    client = _FakeClient()
    result = await _provider(client).answer("anything?", [])

    assert result.text
    assert result.citations == []
    assert result.not_in_vault is True  # the outcome is a flagged refusal
    assert client.messages.calls == []  # the model is never consulted


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
    await _provider(client, model="claude-haiku-4-5").answer("q", [_chunk(0)])

    assert client.messages.calls[0]["model"] == "claude-haiku-4-5"


async def test_default_model_is_opus() -> None:
    client = _FakeClient()
    await _provider(client).answer("q", [_chunk(0)])

    assert client.messages.calls[0]["model"] == "claude-opus-4-8"


async def test_prompt_numbers_chunks_and_grounds_the_model() -> None:
    client = _FakeClient()
    chunks = [_chunk(0), _chunk(1)]
    await _provider(client).answer("what is x?", chunks)

    call = client.messages.calls[0]
    system = call["system"].lower()
    # Grounding instructions: answer only from sources, cite by number, be honest.
    assert "only" in system
    assert "not in this vault" in system

    user_text = call["messages"][0]["content"]
    if not isinstance(user_text, str):  # content may be a list of blocks
        user_text = " ".join(block.get("text", "") for block in user_text)
    # Each retrieved chunk appears under its 1-based marker, in order.
    assert "[1]" in user_text and "passage 0" in user_text
    assert "[2]" in user_text and "passage 1" in user_text
    assert "what is x?" in user_text


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

    # First appearance wins the ordering; duplicates collapse.
    assert [c.number for c in result.citations] == [2, 1]
    assert [c.chunk_id for c in result.citations] == [chunks[1].chunk_id, chunks[0].chunk_id]


async def test_out_of_range_markers_are_ignored() -> None:
    client = _FakeClient(reply="Fabricated [5] and real [1].")
    chunks = [_chunk(0)]
    result = await _provider(client).answer("q", chunks)

    assert [c.number for c in result.citations] == [1]


async def test_uncited_answer_over_chunks_has_no_citations() -> None:
    """Model answered without markers (e.g. couldn't ground it) → no citations.

    An answer that grounds nothing is not a grounded answer, so it is flagged
    ``not_in_vault`` — the honest signal downstream relies on.
    """
    client = _FakeClient(reply="I can't find that in the provided sources.")
    result = await _provider(client).answer("q", [_chunk(0)])

    assert result.text
    assert result.citations == []
    assert result.not_in_vault is True
