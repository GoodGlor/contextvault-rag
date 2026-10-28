"""Chunk parsed documents into overlapping, positioned passages.

The ``chunk`` stage of the ingestion pipeline: turn a
``ParsedDocument`` into overlapping ``TextChunk``s sized for retrieval. Each
chunk keeps the exact character span it was sliced from — so a citation can
jump back to the highlighted source passage — plus the source page(s) that span
touches, derived from the document's positioned blocks.

Chunking is character-based in v1: deterministic, tokenizer-free, and the
offsets map straight onto the ``char_start``/``char_end`` a citation needs, with
the invariant ``parsed.text[chunk.char_start:chunk.char_end] == chunk.text``.
Size and overlap are configurable, defaulting to the ``chunk_size`` /
``chunk_overlap`` settings.
"""

from dataclasses import dataclass

from contextvault.core.config import get_settings
from contextvault.ingestion.parsing import ParsedDocument


@dataclass(frozen=True)
class TextChunk:
    """An overlapping slice of a parsed document, positioned for citation.

    ``char_start`` / ``char_end`` are offsets into ``ParsedDocument.text``;
    ``pages`` are the distinct 1-based source pages the span touches (empty when
    the source has no pagination). ``ordinal`` is the chunk's 0-based position.
    """

    text: str
    ordinal: int
    char_start: int
    char_end: int
    pages: tuple[int, ...]


def _pages_for_span(parsed: ParsedDocument, start: int, end: int) -> tuple[int, ...]:
    """Distinct source pages whose block overlaps the half-open span ``[start, end)``."""
    pages = {
        block.page
        for block in parsed.blocks
        if block.page is not None and block.start < end and block.end > start
    }
    return tuple(sorted(pages))


def chunk_document(
    parsed: ParsedDocument, *, size: int | None = None, overlap: int | None = None
) -> list[TextChunk]:
    """Split ``parsed`` into overlapping ``TextChunk``s.

    ``size`` and ``overlap`` are in characters and default to the configured
    ``chunk_size`` / ``chunk_overlap``. Windows advance by ``size - overlap`` and
    the final window ends exactly at the end of the text, so chunks tile it with
    no redundant tail. Raises ``ValueError`` unless ``0 <= overlap < size``.
    """
    settings = get_settings()
    size = settings.chunk_size if size is None else size
    overlap = settings.chunk_overlap if overlap is None else overlap
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")

    text = parsed.text
    length = len(text)
    if length == 0:
        return []

    step = size - overlap
    chunks: list[TextChunk] = []
    start = 0
    while start < length:
        end = min(start + size, length)
        chunks.append(
            TextChunk(
                text=text[start:end],
                ordinal=len(chunks),
                char_start=start,
                char_end=end,
                pages=_pages_for_span(parsed, start, end),
            )
        )
        if end >= length:
            break
        start += step
    return chunks
