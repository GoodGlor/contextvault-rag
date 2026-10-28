"""Tests for the provider-agnostic generation contract.

These pin the shape of the ``LLMProvider`` interface and the shared
``Answer``/``Citation`` schema without any real provider: a small in-memory
``_FakeProvider`` stands in for a concrete implementation, exercising the
contract the way #16's Anthropic provider (and its siblings) will satisfy it.
"""

import dataclasses
import uuid
from collections.abc import Sequence

import pytest

from contextvault.llm import Answer, Citation, LLMProvider
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


class _FakeProvider:
    """Reference in-memory implementation of the ``LLMProvider`` contract.

    Numbers the retrieved chunks ``[1..n]`` and cites every one, mirroring the
    provider-agnostic scheme; with no chunks it returns the honest "not in this
    vault" answer and no citations.
    """

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        if not chunks:
            return Answer(text="I don't have that in this vault.", citations=[])
        citations = [
            Citation(
                number=i,
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            )
            for i, chunk in enumerate(chunks, start=1)
        ]
        body = " ".join(f"[{c.number}]" for c in citations)
        return Answer(text=f"Answer to {question!r} {body}", citations=citations)


def test_fake_provider_satisfies_protocol() -> None:
    provider: LLMProvider = _FakeProvider()
    assert isinstance(provider, LLMProvider)


def test_non_conforming_object_is_not_a_provider() -> None:
    assert not isinstance(object(), LLMProvider)


def test_answer_and_citation_are_frozen() -> None:
    citation = Citation(
        number=1,
        chunk_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        char_start=0,
        char_end=10,
    )
    answer = Answer(text="hi", citations=[citation])
    with pytest.raises(dataclasses.FrozenInstanceError):
        answer.text = "changed"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        citation.number = 2  # type: ignore[misc]


async def test_answer_cites_each_chunk_by_number() -> None:
    chunks = [_chunk(0), _chunk(1)]
    result = await _FakeProvider().answer("what is x?", chunks)

    assert [c.number for c in result.citations] == [1, 2]
    # Each citation maps back to its chunk's source span.
    assert [(c.chunk_id, c.char_start, c.char_end) for c in result.citations] == [
        (chunks[0].chunk_id, chunks[0].char_start, chunks[0].char_end),
        (chunks[1].chunk_id, chunks[1].char_start, chunks[1].char_end),
    ]
    assert "[1]" in result.text and "[2]" in result.text


async def test_empty_chunks_yields_uncited_answer() -> None:
    """The honest 'not in this vault' path: text, no citations."""
    result = await _FakeProvider().answer("anything?", [])

    assert result.text
    assert result.citations == []


def test_answer_is_grounded_by_default() -> None:
    """A cited answer is a grounded one: ``not_in_vault`` is off unless set."""
    answer = Answer(text="hi", citations=[])
    assert answer.not_in_vault is False


def test_answer_can_flag_not_in_vault() -> None:
    answer = Answer(text="nope", citations=[], not_in_vault=True)
    assert answer.not_in_vault is True


def test_citation_allows_missing_offsets() -> None:
    """Chunks parsed without positions still cite (span is None)."""
    citation = Citation(
        number=1,
        chunk_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        char_start=None,
        char_end=None,
    )
    assert citation.char_start is None and citation.char_end is None
