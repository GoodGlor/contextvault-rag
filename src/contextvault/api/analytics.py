"""Admin query-analytics endpoint.

One composite `GET /analytics` feeds the usage dashboard: totals + answered/gap
rate, per-repository volume, most-asked questions, most-active users, and the
answered-vs-gap rate over time. Read-only aggregation over the query log (#30).
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.db.session import get_session
from contextvault.models import User
from contextvault.services import analytics as analytics_service

router = APIRouter(tags=["analytics"])


class RepositoryVolumeResponse(BaseModel):
    repository_id: UUID
    repository_name: str
    query_count: int
    not_in_vault_count: int


class QuestionCountResponse(BaseModel):
    question: str
    ask_count: int


class UserActivityResponse(BaseModel):
    user_id: UUID
    username: str
    query_count: int


class DailyVolumeResponse(BaseModel):
    day: date
    total: int
    not_in_vault: int


class AnalyticsOverviewResponse(BaseModel):
    """The composite analytics summary for the admin dashboard."""

    total_queries: int
    answered: int
    not_in_vault: int
    not_in_vault_rate: float
    per_repository: list[RepositoryVolumeResponse]
    top_questions: list[QuestionCountResponse]
    active_users: list[UserActivityResponse]
    by_day: list[DailyVolumeResponse]


@router.get("/analytics")
async def get_analytics(
    top_limit: int = Query(default=10, ge=1, le=100),
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AnalyticsOverviewResponse:
    """Usage analytics over all logged queries (admin-only). ``top_limit`` bounds the
    most-asked-questions and most-active-users lists."""
    overview = await analytics_service.get_overview(session, top_limit=top_limit)
    return AnalyticsOverviewResponse(
        total_queries=overview.total_queries,
        answered=overview.answered,
        not_in_vault=overview.not_in_vault,
        not_in_vault_rate=overview.not_in_vault_rate,
        per_repository=[
            RepositoryVolumeResponse(
                repository_id=r.repository_id,
                repository_name=r.repository_name,
                query_count=r.query_count,
                not_in_vault_count=r.not_in_vault_count,
            )
            for r in overview.per_repository
        ],
        top_questions=[
            QuestionCountResponse(question=q.question, ask_count=q.ask_count)
            for q in overview.top_questions
        ],
        active_users=[
            UserActivityResponse(user_id=u.user_id, username=u.username, query_count=u.query_count)
            for u in overview.active_users
        ],
        by_day=[
            DailyVolumeResponse(day=d.day, total=d.total, not_in_vault=d.not_in_vault)
            for d in overview.by_day
        ],
    )
