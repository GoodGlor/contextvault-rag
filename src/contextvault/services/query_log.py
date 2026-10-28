"""Query-logging service.

``log_query`` persists one ``QueryLog`` row per answered question. It is called from
the query endpoint after retrieval + generation, capturing the retrieval signal
(``top_score``, ``chunk_count``) and whether the answer was grounded
(``not_in_vault``). Downstream cards read these rows: the knowledge-gap dashboard
(#31) and usage analytics (#33).
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from contextvault.models import QueryLog


def normalized_question(
    column: sa.ColumnElement[str] | InstrumentedAttribute[str],
) -> sa.ColumnElement[str]:
    """Normalize a question column for grouping "the same" question together.

    Lowercase, trim, and collapse internal whitespace runs to a single space — a
    deliberate v1 heuristic (not semantic clustering) that merges obvious re-asks.
    Shared by knowledge-gap aggregation (#31) and analytics (#33) so both group
    identically.
    """
    return sa.func.regexp_replace(sa.func.lower(sa.func.btrim(column)), r"\s+", " ", "g")


async def log_query(
    session: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    repository_id: uuid.UUID,
    question: str,
    top_score: float | None,
    chunk_count: int,
    not_in_vault: bool,
) -> QueryLog:
    """Record a query and its retrieval outcome. Adds + flushes; the caller commits."""
    entry = QueryLog(
        user_id=user_id,
        repository_id=repository_id,
        question=question,
        top_score=top_score,
        chunk_count=chunk_count,
        not_in_vault=not_in_vault,
    )
    session.add(entry)
    await session.flush()
    return entry
