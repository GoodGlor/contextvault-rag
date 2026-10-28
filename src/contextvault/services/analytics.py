"""Query analytics aggregations.

Turns the query log (#30) into the usage insight an admin dashboard needs:
most-asked questions, per-repository volume, the most active users, and the
answered-vs-"not in vault" rate over time. All read-only aggregation over
``query_logs``; ``get_overview`` returns one composite object so the dashboard is a
single call.
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import QueryLog, Repository, User
from contextvault.services.query_log import normalized_question

# count(*) FILTER (WHERE not_in_vault) — the gap tally alongside the total count.
_GAP_COUNT = sa.func.count().filter(QueryLog.not_in_vault.is_(True))


@dataclass(frozen=True)
class RepositoryVolume:
    """Query volume for one repository — "which repos are active"."""

    repository_id: UUID
    repository_name: str
    query_count: int
    not_in_vault_count: int


@dataclass(frozen=True)
class QuestionCount:
    """A most-asked question (aggregated case/whitespace-insensitively)."""

    question: str
    ask_count: int


@dataclass(frozen=True)
class UserActivity:
    """A most-active known user — "who's using what". Anonymized rows are excluded."""

    user_id: UUID
    username: str
    query_count: int


@dataclass(frozen=True)
class DailyVolume:
    """One day's totals — the answered-vs-gap rate over time."""

    day: date
    total: int
    not_in_vault: int


@dataclass(frozen=True)
class AnalyticsOverview:
    """The full analytics summary the admin dashboard renders."""

    total_queries: int
    answered: int
    not_in_vault: int
    not_in_vault_rate: float
    per_repository: list[RepositoryVolume]
    top_questions: list[QuestionCount]
    active_users: list[UserActivity]
    by_day: list[DailyVolume]


async def _totals(session: AsyncSession) -> tuple[int, int]:
    """Return (total_queries, not_in_vault_count) across all logs."""
    row = (await session.execute(sa.select(sa.func.count(), _GAP_COUNT))).one()
    return int(row[0]), int(row[1])


async def _per_repository(session: AsyncSession) -> list[RepositoryVolume]:
    query_count = sa.func.count().label("query_count")
    stmt = (
        sa.select(
            Repository.id,
            Repository.name,
            query_count,
            _GAP_COUNT.label("not_in_vault_count"),
        )
        .select_from(QueryLog)
        .join(Repository, Repository.id == QueryLog.repository_id)
        .group_by(Repository.id, Repository.name)
        .order_by(query_count.desc(), Repository.name)
    )
    rows = (await session.execute(stmt)).all()
    return [
        RepositoryVolume(
            repository_id=r.id,
            repository_name=r.name,
            query_count=r.query_count,
            not_in_vault_count=r.not_in_vault_count,
        )
        for r in rows
    ]


async def _top_questions(session: AsyncSession, limit: int) -> list[QuestionCount]:
    ask_count = sa.func.count().label("ask_count")
    stmt = (
        sa.select(sa.func.min(QueryLog.question).label("question"), ask_count)
        .group_by(normalized_question(QueryLog.question))
        .order_by(ask_count.desc(), sa.func.min(QueryLog.question))
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [QuestionCount(question=r.question, ask_count=r.ask_count) for r in rows]


async def _active_users(session: AsyncSession, limit: int) -> list[UserActivity]:
    query_count = sa.func.count().label("query_count")
    stmt = (
        sa.select(User.id, User.username, query_count)
        .select_from(QueryLog)
        .join(User, User.id == QueryLog.user_id)
        .group_by(User.id, User.username)
        .order_by(query_count.desc(), User.username)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [
        UserActivity(user_id=r.id, username=r.username, query_count=r.query_count) for r in rows
    ]


async def _by_day(session: AsyncSession) -> list[DailyVolume]:
    day = sa.cast(sa.func.date_trunc("day", QueryLog.created_at), sa.Date).label("day")
    stmt = (
        sa.select(day, sa.func.count().label("total"), _GAP_COUNT.label("not_in_vault"))
        .group_by(day)
        .order_by(day)
    )
    rows = (await session.execute(stmt)).all()
    return [DailyVolume(day=r.day, total=r.total, not_in_vault=r.not_in_vault) for r in rows]


async def get_overview(session: AsyncSession, *, top_limit: int = 10) -> AnalyticsOverview:
    """Compute the composite analytics overview.

    ``top_limit`` bounds the most-asked-questions and most-active-users lists.
    """
    total, gaps = await _totals(session)
    return AnalyticsOverview(
        total_queries=total,
        answered=total - gaps,
        not_in_vault=gaps,
        not_in_vault_rate=(gaps / total if total else 0.0),
        per_repository=await _per_repository(session),
        top_questions=await _top_questions(session, top_limit),
        active_users=await _active_users(session, top_limit),
        by_day=await _by_day(session),
    )
