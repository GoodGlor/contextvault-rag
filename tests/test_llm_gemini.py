"""Tests for the Google (Gemini) ``LLMProvider``.

The Google GenAI SDK is never called for real: a fake stands in for
``genai.Client``, capturing the request the provider builds (``model``,
``contents``, ``config``) and returning a canned response. That pins the same
behaviours the Anthropic provider guarantees — numbered-chunk grounding, answer
only from the sources, honest "not in this vault" short-circuit, and ``[n]``
markers mapped back to source chunks — without a network call or an API key.
"""

import uuid
from typing import Any, cast

from google import genai

from contextvault.llm import LLMProvider
from contextvault.llm.gemini import GeminiLLMProvider
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


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModels:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return _FakeResponse(self._reply)


class _FakeAio:
    def __init__(self, reply: str) -> None:
        self.models = _FakeModels(reply)


class _FakeClient:
    """Structural stand-in for ``genai.Client`` — only ``.aio.models.generate_content``."""

    def __init__(self, reply: str = "grounded answer [1]") -> None:
        self.aio = _FakeAio(reply)


def _provider(client: _FakeClient, **kwargs: Any) -> GeminiLLMProvider:
    return GeminiLLMProvider(client=cast(genai.Client, client), **kwargs)


def test_provider_satisfies_protocol() -> None:
    provider: LLMProvider = _provider(_FakeClient())
    assert isinstance(provider, LLMProvider)


async def test_empty_chunks_short_circuits_without_calling_api() -> None:
    client = _FakeClient()
    result = await _provider(client).answer("anything?", [])

    assert result.text
    assert result.citations == []
    assert result.not_in_vault is True  # the outcome is a flagged refusal
    assert client.aio.models.calls == []  # the model is never consulted


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
    await _provider(client, model="gemini-1.5-flash").answer("q", [_chunk(0)])

    assert client.aio.models.calls[0]["model"] == "gemini-1.5-flash"


async def test_default_model_is_gemini() -> None:
    client = _FakeClient()
    await _provider(client).answer("q", [_chunk(0)])

    assert client.aio.models.calls[0]["model"] == "gemini-2.5-flash"


async def test_prompt_numbers_chunks_and_grounds_the_model() -> None:
    client = _FakeClient()
    chunks = [_chunk(0), _chunk(1)]
    await _provider(client).answer("what is x?", chunks)

    call = client.aio.models.calls[0]
    system = call["config"].system_instruction.lower()
    assert "only" in system
    assert "not in this vault" in system

    contents = call["contents"]
    assert "[1]" in contents and "passage 0" in contents
    assert "[2]" in contents and "passage 1" in contents
    assert "what is x?" in contents


async def test_max_tokens_passed_in_config() -> None:
    client = _FakeClient()
    await _provider(client, max_tokens=321).answer("q", [_chunk(0)])

    assert client.aio.models.calls[0]["config"].max_output_tokens == 321


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


async def test_blank_response_text_yields_empty_answer() -> None:
    """Gemini's ``response.text`` can be empty/None (e.g. a blocked response).

    Grounding nothing, it is flagged ``not_in_vault`` rather than passed off as a
    grounded answer.
    """
    client = _FakeClient(reply="")
    result = await _provider(client).answer("q", [_chunk(0)])

    assert result.text == ""
    assert result.citations == []
    assert result.not_in_vault is True
