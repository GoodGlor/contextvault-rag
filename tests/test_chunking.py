"""Tests for chunking parsed documents into overlapping, positioned passages.

Chunking is character-based, so the strongest invariant is that a chunk's
offsets slice its exact text back out of the source — that is what later lets a
citation jump to the highlighted passage.
"""

import pytest

from contextvault.core.config import get_settings
from contextvault.ingestion import (
    ParsedDocument,
    TextBlock,
    TextChunk,
    chunk_document,
)


def _one_block_doc(text: str, page: int | None = None) -> ParsedDocument:
    block = TextBlock(text=text, start=0, end=len(text), page=page)
    return ParsedDocument(text=text, blocks=(block,))


def test_offsets_slice_back_to_source_text() -> None:
    text = "abcdefghij" * 20  # 200 chars
    parsed = _one_block_doc(text)
    chunks = chunk_document(parsed, size=50, overlap=10)
    assert len(chunks) > 1
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_chunks_are_ordered_and_cover_whole_text() -> None:
    text = "x" * 205
    chunks = chunk_document(_one_block_doc(text), size=50, overlap=10)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(text)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.char_start > prev.char_start  # advancing
        assert nxt.char_start < prev.char_end  # while overlapping


def test_overlap_step_is_respected() -> None:
    text = "y" * 100
    chunks = chunk_document(_one_block_doc(text), size=40, overlap=10)
    step = 40 - 10
    for i, chunk in enumerate(chunks[:-1]):
        assert chunk.char_start == i * step


def test_short_text_is_a_single_chunk() -> None:
    text = "short"
    chunks = chunk_document(_one_block_doc(text), size=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)
    assert chunks[0].text == text


def test_empty_text_yields_no_chunks() -> None:
    assert chunk_document(_one_block_doc(""), size=100, overlap=10) == []


def test_no_redundant_trailing_chunk() -> None:
    # A final window fully inside the previous one adds nothing and must not appear.
    text = "z" * 85
    chunks = chunk_document(_one_block_doc(text), size=50, overlap=10)
    assert chunks[-1].char_end == len(text)
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.char_end > prev.char_end  # every chunk adds new ground


def test_pages_span_block_boundaries() -> None:
    p1, p2 = "A" * 30, "B" * 30
    text = p1 + p2
    blocks = (
        TextBlock(text=p1, start=0, end=30, page=1),
        TextBlock(text=p2, start=30, end=60, page=2),
    )
    parsed = ParsedDocument(text=text, blocks=blocks)
    chunks = chunk_document(parsed, size=40, overlap=5)
    # First chunk [0, 40) straddles both pages; a later one sits only on page 2.
    assert chunks[0].pages == (1, 2)
    assert chunks[-1].pages == (2,)


def test_pageless_blocks_yield_empty_pages() -> None:
    chunks = chunk_document(_one_block_doc("hello world", page=None), size=100, overlap=10)
    assert chunks[0].pages == ()


def test_invalid_size_or_overlap_raise() -> None:
    parsed = _one_block_doc("abc")
    with pytest.raises(ValueError):
        chunk_document(parsed, size=0, overlap=0)
    with pytest.raises(ValueError):
        chunk_document(parsed, size=10, overlap=10)  # overlap must be < size
    with pytest.raises(ValueError):
        chunk_document(parsed, size=10, overlap=-1)


def test_defaults_come_from_settings() -> None:
    settings = get_settings()
    assert settings.chunk_size > settings.chunk_overlap >= 0
    text = "w" * (settings.chunk_size * 3)
    chunks = chunk_document(_one_block_doc(text))
    assert len(chunks) >= 3
    for chunk in chunks:
        assert text[chunk.char_start : chunk.char_end] == chunk.text
    assert isinstance(chunks[0], TextChunk)
