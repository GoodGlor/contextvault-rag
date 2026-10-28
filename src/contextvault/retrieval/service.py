"""Retrieval service — question in, ranked relevant chunks out.

This is the orchestration layer the RAG loop calls. It embeds
the question with the system-wide provider, runs the access-filtered vector
search (``search_chunks`` — the SQL-level access boundary), then applies a
relevance threshold so the *weak/empty* case is distinguishable from a real hit.
That distinction is what powers the honest "not in this vault" answer and the
knowledge-gap dashboard: ``top_score`` is set whenever any chunk was retrievable
(so a weak-but-present match feeds the gap signal), while ``chunks`` holds only
the matches good enough to answer from.
"""

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.embeddings.base import EmbeddingProvider
from contextvault.retrieval.search import RetrievedChunk, search_chunks


@dataclass(frozen=True)
class RetrievalResult:
    """Outcome of a retrieval: the relevant chunks plus the weak/empty signal.

    ``chunks`` are the hits meeting the relevance threshold, ranked closest
    first. ``top_score`` is the best similarity among *all* retrievable chunks
    (before thresholding), or ``None`` when nothing was retrievable at all —
    letting a caller tell "no source covers this" (``top_score`` set, ``chunks``
    empty) apart from "empty/inaccessible vault" (``top_score`` ``None``).
    """

    chunks: list[RetrievedChunk]
    top_score: float | None

    @property
    def has_results(self) -> bool:
        """True when at least one chunk cleared the relevance threshold."""
        return bool(self.chunks)


async def retrieve(
    session: AsyncSession,
    *,
    question: str,
    repository_id: uuid.UUID,
    user_id: uuid.UUID,
    embedder: EmbeddingProvider,
    k: int | None = None,
    min_score: float | None = None,
) -> RetrievalResult:
    """Embed ``question`` and return the relevant chunks the user may read.

    Runs the access-filtered search scoped to ``repository_id`` (empty unless
    ``user_id`` holds an active grant), then keeps only hits whose cosine
    similarity is at least ``min_score`` (defaulting to ``retrieval_min_score``).
    ``k`` bounds how many chunks the search considers before thresholding and
    defaults to ``retrieval_top_k``.
    """
    threshold = min_score if min_score is not None else get_settings().retrieval_min_score

    # ``embed`` is a synchronous network call; keep the event loop free.
    vectors = await asyncio.to_thread(lambda: embedder.embed([question], task="query"))
    query_embedding = vectors[0]

    hits = await search_chunks(
        session,
        user_id=user_id,
        repository_id=repository_id,
        query_embedding=query_embedding,
        k=k,
    )

    top_score = hits[0].score if hits else None
    relevant = [hit for hit in hits if hit.score >= threshold]
    return RetrievalResult(chunks=relevant, top_score=top_score)
