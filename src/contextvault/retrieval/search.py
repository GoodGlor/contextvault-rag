"""Access-filtered vector search — the RAG loop's core access boundary.

``search_chunks`` is a single SQL query that joins ``chunks`` to ``grants`` and
returns the top-k most similar chunks *for a repository the user holds an active
grant on*. The permission filter and the similarity search happen together in
the query, so a user can never retrieve from a repo they weren't granted — the
boundary lives in SQL, not in app code layered on top.

Similarity is cosine (matching the ANN index's ``vector_cosine_ops``); the
returned ``score`` is cosine similarity in ``[-1, 1]``, higher meaning closer.
This is the raw query layer; the question→embed→search service sits above it.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.models import Chunk, Grant, Source, SourceStatus


@dataclass(frozen=True)
class RetrievedChunk:
    """One search hit: the chunk, its citation offsets, and its similarity score."""

    chunk_id: uuid.UUID
    source_id: uuid.UUID
    repository_id: uuid.UUID
    ordinal: int
    content: str
    char_start: int | None
    char_end: int | None
    score: float


async def search_chunks(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    repository_id: uuid.UUID,
    query_embedding: Sequence[float],
    k: int | None = None,
) -> list[RetrievedChunk]:
    """Return the ``k`` most similar chunks in ``repository_id`` the user may read.

    The result is empty unless ``user_id`` holds a grant on ``repository_id``
    that has not expired — this is the access guarantee, enforced by the join to
    ``grants`` rather than a separate check. Chunks without an embedding are
    skipped. ``k`` defaults to the ``retrieval_top_k`` setting.
    """
    top_k = k if k is not None else get_settings().retrieval_top_k

    distance = Chunk.embedding.cosine_distance(list(query_embedding))
    stmt = (
        sa.select(
            Chunk.id,
            Chunk.source_id,
            Chunk.repository_id,
            Chunk.ordinal,
            Chunk.content,
            Chunk.char_start,
            Chunk.char_end,
            (1 - distance).label("score"),
        )
        .join(Grant, Grant.repository_id == Chunk.repository_id)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            Chunk.repository_id == repository_id,
            Chunk.embedding.is_not(None),
            # Only sources that finished ingesting. Chunks are written slice by slice
            # and deliberately survive a failure so a retry can resume from where it
            # stopped, so rows exist for sources that never completed; without this
            # they would answer questions from a fragment of the document.
            Source.status == SourceStatus.DONE,
            Grant.user_id == user_id,
            sa.or_(Grant.expires_at.is_(None), Grant.expires_at > sa.func.now()),
        )
        .order_by(distance)
        .limit(top_k)
    )

    result = await session.execute(stmt)
    return [
        RetrievedChunk(
            chunk_id=row.id,
            source_id=row.source_id,
            repository_id=row.repository_id,
            ordinal=row.ordinal,
            content=row.content,
            char_start=row.char_start,
            char_end=row.char_end,
            score=float(row.score),
        )
        for row in result
    ]
