"""Knowledge-gap detection.

A *knowledge gap* is a question the vault could not answer — a logged query whose
answer was the honest "not in this vault" (``not_in_vault = True``: retrieval was
empty or too weak to ground). This service turns those logged gaps into the admin's
curation to-do list for one repository: similar questions are aggregated (case- and
whitespace-insensitive) and ranked so the most-asked, still-uncovered topics rise to
the top — "N users asked about X, no source covers it".

The loop this feeds: user demand (gaps) → admin writes an Admin Note (#32) → the
vault permanently answers it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import GapRejection, QueryLog
from contextvault.services.query_log import normalized_question

# Group questions that differ only in case or whitespace (shared with analytics #33).
_NORMALIZED_QUESTION = normalized_question(QueryLog.question)


@dataclass(frozen=True)
class KnowledgeGap:
    """One aggregated gap topic for the admin dashboard.

    ``question`` is a representative original phrasing (gaps are grouped case- and
    whitespace-insensitively). ``ask_count`` is how many times it was asked;
    ``user_count`` is the distinct *known* askers (anonymized/deleted users are not
    counted distinctly). ``last_asked_at`` is the most recent occurrence.
    """

    question: str
    ask_count: int
    user_count: int
    last_asked_at: datetime


async def list_knowledge_gaps(
    session: AsyncSession, repository_id: UUID, *, limit: int | None = None
) -> Sequence[KnowledgeGap]:
    """Ranked knowledge gaps for a repository — most-asked (then most-recent) first."""
    ask_count = sa.func.count().label("ask_count")
    last_asked_at = sa.func.max(QueryLog.created_at).label("last_asked_at")
    rejected = sa.select(GapRejection.normalized_question).where(
        GapRejection.repository_id == repository_id
    )
    stmt = (
        sa.select(
            sa.func.min(QueryLog.question).label("question"),
            ask_count,
            sa.func.count(sa.distinct(QueryLog.user_id)).label("user_count"),
            last_asked_at,
        )
        .where(
            QueryLog.repository_id == repository_id,
            QueryLog.not_in_vault.is_(True),
            _NORMALIZED_QUESTION.notin_(rejected),
        )
        .group_by(_NORMALIZED_QUESTION)
        .order_by(ask_count.desc(), last_asked_at.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = (await session.execute(stmt)).all()
    return [
        KnowledgeGap(
            question=row.question,
            ask_count=row.ask_count,
            user_count=row.user_count,
            last_asked_at=row.last_asked_at,
        )
        for row in rows
    ]


def _normalize_text(question: str) -> str:
    """Python twin of ``normalized_question`` (SQL) for storing the gap identity.

    Mirrors SQL exactly: ``btrim()`` (default) trims ONLY ASCII spaces from the
    edges — not all whitespace — so the edge-trim here must be spaces-only too.
    Any leading/trailing tab or newline is left in place for the subsequent
    ``\\s+`` → single-space collapse to normalize, exactly as SQL's
    ``regexp_replace(lower(btrim(column)), '\\s+', ' ', 'g')`` does. Using
    ``str.strip()`` (which trims all whitespace) here would diverge from SQL for
    questions with edge tabs/newlines.
    """
    return re.sub(r"\s+", " ", question.strip(" ").lower())


async def reject_gap(
    session: AsyncSession,
    repository_id: UUID,
    *,
    question: str,
    reason: str,
    admin_id: UUID | None,
) -> GapRejection:
    """Reject a gap (upsert on repo + normalized question); the caller commits."""
    normalized = _normalize_text(question)
    existing = (
        await session.execute(
            sa.select(GapRejection).where(
                GapRejection.repository_id == repository_id,
                GapRejection.normalized_question == normalized,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.question = question
        existing.reason = reason
        existing.rejected_by = admin_id
        await session.flush()
        return existing
    rejection = GapRejection(
        repository_id=repository_id,
        normalized_question=normalized,
        question=question,
        reason=reason,
        rejected_by=admin_id,
    )
    session.add(rejection)
    await session.flush()
    return rejection


async def list_rejected_gaps(session: AsyncSession, repository_id: UUID) -> Sequence[GapRejection]:
    """Rejected gaps for a repository, newest first."""
    rows = (
        (
            await session.execute(
                sa.select(GapRejection)
                .where(GapRejection.repository_id == repository_id)
                .order_by(GapRejection.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)
