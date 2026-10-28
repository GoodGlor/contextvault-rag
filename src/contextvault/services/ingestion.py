"""Ingestion pipeline orchestration: parse → chunk → embed → store.

Ties together the parse stage, the chunk stage and the
``EmbeddingProvider`` to turn an uploaded document into stored,
embedded ``Chunk``s. A source carries its own ingestion
``status`` (pending → processing → done/failed); any failure is captured on the
source as ``ingest_error`` so it is recorded, never silent.

``ingest_source`` is the awaitable core. ``run_ingestion`` is the thin seam a
request handler schedules via FastAPI ``BackgroundTasks``: it opens its own
session (the request's is gone by the time it runs) and delegates.
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import SessionLocal
from contextvault.embeddings.base import EmbeddingProvider
from contextvault.ingestion import (
    IMAGE_SUFFIXES,
    ParsedDocument,
    TextChunk,
    chunk_document,
    file_suffix,
    parse_document,
    parsed_from_text,
)
from contextvault.llm.ocr import transcribe_image
from contextvault.models import Chunk, Repository, Source, SourceStatus
from contextvault.services import providers as provider_service
from contextvault.services.web_source import extract_web_text, fetch_html

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# Chunks embedded per progress checkpoint. Matches the provider's own request batch
# size, so a slice is one upstream call and progress ticks once per request.
EMBED_PROGRESS_SLICE = 100


async def ingest_source(
    session: AsyncSession,
    source: Source,
    *,
    filename: str,
    data: bytes,
    embedder: EmbeddingProvider,
) -> None:
    """Run the full pipeline for ``source`` and commit the result.

    Parses ``data``, chunks it, embeds every chunk, and replaces any prior
    chunks for the source (so re-ingest is idempotent). On success the source is
    marked ``DONE`` with its extracted text stored; on any error it is marked
    ``FAILED`` with the error captured in ``ingest_error`` — the failure is
    persisted rather than raised, so a caller inspects ``source.status``.
    """
    source_id = source.id

    source.status = SourceStatus.PROCESSING
    source.ingest_error = None
    await session.commit()

    try:
        if file_suffix(filename) in IMAGE_SUFFIXES:
            parsed = await _ocr_image(session, source, data)
        else:
            # Off the event loop: extracting text from a multi-megabyte PDF takes
            # seconds to minutes of pure CPU, and running it inline froze the whole
            # dyno — every other request, including the admin UI's own status polls,
            # waited behind it.
            parsed = await asyncio.to_thread(parse_document, filename, data)
        await store_parsed(session, source, parsed, embedder)
    except Exception as exc:
        await _record_failure(session, source_id, exc)


async def _ocr_image(session: AsyncSession, source: Source, data: bytes) -> ParsedDocument:
    """Transcribe an image source with the repository's configured vision model.

    Images are read by the repo's own LLM (Cyrillic-capable, unlike the old local OCR)
    using the provider's global key. Requires the repo to be answerable — a model
    picked whose provider has a verified key — otherwise the source fails with a clear,
    actionable message. Empty transcriptions are a failure too, not a silent success.
    """
    repo = await session.get(Repository, source.repository_id)
    if repo is None or not await provider_service.repo_is_answerable(session, repo):
        raise ValueError(
            "This repository has no usable model for reading images. Pick a model whose "
            "provider has a verified API key, then re-upload."
        )
    assert repo.llm_provider is not None and repo.llm_model is not None
    provider, model = repo.llm_provider.value, repo.llm_model
    api_key, base_url = await provider_service.get_call_credentials(session, repo.llm_provider)
    # Release the pooled DB connection before the slow vision call: loading the repo
    # and key opened a read transaction that would otherwise stay pinned across the
    # whole OCR (and the embed that follows), so bulk image uploads exhaust the pool
    # and every other request times out (QueuePool limit reached). ``expire_on_commit``
    # is off, so ``source``/``repo`` stay usable afterward.
    await session.commit()
    text = await transcribe_image(provider, api_key, model, image=data, base_url=base_url)
    if not text.strip():
        raise ValueError("No text found in image.")
    return parsed_from_text(text)


async def store_parsed(
    session: AsyncSession,
    source: Source,
    parsed: object,
    embedder: EmbeddingProvider,
) -> None:
    """Chunk → embed → replace-chunks → mark DONE for an already-parsed source.

    The single writer of chunks, shared by document/image ingestion and web
    ingestion. ``parsed`` is a ``ParsedDocument``. Commits on success.
    """
    chunks = chunk_document(parsed)  # type: ignore[arg-type]

    # Anything a previous run already embedded and that still matches is kept, so a
    # retry after a quota failure pays only for the missing tail.
    reusable = await _reusable_prefix(session, source, chunks)

    # Publish the denominator before embedding starts, so the very first status poll
    # can already draw a bar rather than an indeterminate spinner.
    source.chunks_total = len(chunks)
    source.chunks_done = reusable
    # Everything past the reusable prefix is stale — a changed document, or a longer
    # previous run. New rows land slice by slice rather than in one final flush, so
    # old and new must never coexist.
    await session.execute(
        sa.delete(Chunk).where(Chunk.source_id == source.id, Chunk.ordinal >= reusable)
    )
    await session.commit()

    await _embed_and_store(session, source, chunks, embedder, start_at=reusable)

    source.content = parsed.text  # type: ignore[attr-defined]
    source.status = SourceStatus.DONE
    source.ingest_error = None
    await session.commit()


async def _reusable_prefix(session: AsyncSession, source: Source, chunks: list[TextChunk]) -> int:
    """How many leading chunks are already embedded and still match ``chunks``.

    Embedding is the expensive, rate-limited step, and under a daily quota a retry
    that re-embeds from zero re-spends the whole document — which is how an
    allowance disappears. Chunks are written as they are embedded and survive a
    failure, so a retry can pick up where it stopped.

    Reuse is a *prefix* match on the stored text, not a count: if the document or
    the chunking settings changed, the first mismatch ends it and everything from
    there is re-embedded. That keeps resuming safe without tracking a content hash.
    """
    result = await session.execute(
        sa.select(Chunk.ordinal, Chunk.content)
        .where(Chunk.source_id == source.id)
        .order_by(Chunk.ordinal)
    )
    prefix = 0
    for (ordinal, content), chunk in zip(result.all(), chunks, strict=False):
        if ordinal != prefix or content != chunk.text:
            break
        prefix += 1
    return prefix


async def _embed_and_store(
    session: AsyncSession,
    source: Source,
    chunks: list[TextChunk],
    embedder: EmbeddingProvider,
    *,
    start_at: int = 0,
) -> None:
    """Embed ``chunks`` slice by slice, writing each slice's rows as it lands.

    A multi-megabyte document is thousands of chunks and minutes of embedding, so
    the work is sliced: progress is persisted between slices — that is what the
    admin UI's percentage reads — and each slice's ``Chunk`` rows are written and
    released rather than accumulated. Holding every vector until the end cost
    roughly 200 MB of Python floats for a 7,000-chunk document, which a 512 MB dyno
    cannot afford alongside the parsed text.

    Embedding is synchronous and slow, so each slice runs off the event loop; the
    commit before it also releases the pooled DB connection for the duration, the
    same care ``_ocr_image`` takes.

    Slicing does not reintroduce request bursts: the Gemini provider's pacing is
    process-wide, so its quota spacing spans these slices and every other ingest.
    """
    for start in range(start_at, len(chunks), EMBED_PROGRESS_SLICE):
        part = chunks[start : start + EMBED_PROGRESS_SLICE]
        texts = [c.text for c in part]
        vectors = await asyncio.to_thread(lambda t=texts: embedder.embed(t, task="document"))  # type: ignore[misc]
        session.add_all(
            [
                Chunk(
                    source_id=source.id,
                    repository_id=source.repository_id,
                    ordinal=chunk.ordinal,
                    content=chunk.text,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    embedding=vector,
                )
                for chunk, vector in zip(part, vectors, strict=True)
            ]
        )
        source.chunks_done = start + len(part)
        await session.commit()


async def _record_failure(session: AsyncSession, source_id: uuid.UUID, exc: Exception) -> None:
    """Roll back partial writes, discard partial chunks, and persist the failure.

    Logged as well as stored: the message on the source is what the admin UI shows,
    but a failure that exists only in a database row is invisible in `heroku logs`,
    which makes production diagnosis guesswork.
    """
    logger.error("Ingestion failed for source %s: %s", source_id, exc, exc_info=exc)
    await session.rollback()
    failed = await session.get(Source, source_id)
    if failed is not None:
        # Chunks embedded before the failure are deliberately kept: they were paid
        # for against a rate-limited, daily-capped API, and re-ingest resumes from
        # them instead of re-spending the whole document. They are safe to keep
        # because retrieval only reads chunks of DONE sources.
        failed.status = SourceStatus.FAILED
        failed.ingest_error = f"{type(exc).__name__}: {exc}"
        await session.commit()


async def run_ingestion(
    source_id: uuid.UUID,
    *,
    filename: str,
    data: bytes,
    embedder: EmbeddingProvider,
    session_factory: SessionFactory = SessionLocal,
) -> None:
    """Background-task seam: open a fresh session and ingest ``source_id``.

    Suitable for ``BackgroundTasks.add_task`` — the request-scoped session is
    already closed by the time this runs, so it opens its own. A no-op if the
    source has since been deleted. ``session_factory`` is injectable for tests.
    """
    async with session_factory() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return
        await ingest_source(session, source, filename=filename, data=data, embedder=embedder)


async def run_web_ingestion(
    source_id: uuid.UUID,
    *,
    url: str,
    embedder: EmbeddingProvider,
    session_factory: SessionFactory = SessionLocal,
) -> None:
    """Background-task seam: fetch ``url``, extract its text, and ingest ``source_id``.

    Mirrors :func:`run_ingestion` for web-link sources — opens its own session,
    marks the source PROCESSING, fetches + extracts (off the event loop), and
    stores via :func:`store_parsed`. Any failure is captured on the source.
    """
    async with session_factory() as session:
        source = await session.get(Source, source_id)
        if source is None:
            return

        source.status = SourceStatus.PROCESSING
        source.ingest_error = None
        await session.commit()

        try:
            html = await asyncio.to_thread(fetch_html, url)
            text, title = await asyncio.to_thread(extract_web_text, html)
            if not text.strip():
                raise ValueError("No readable text found at URL.")
            if title:
                source.title = title
            await store_parsed(session, source, parsed_from_text(text), embedder)
        except Exception as exc:
            await _record_failure(session, source_id, exc)
