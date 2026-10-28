"""Admin knowledge-gap dashboard endpoint.

Surfaces the questions a repository could not answer — ranked, aggregated — as the
admin's curation to-do list. Reads the query log written by #30; the admin acts on a
gap by writing an Admin Note (#32), which permanently closes it.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.db.session import get_session
from contextvault.models import Repository, User
from contextvault.services import knowledge_gaps as gap_service

router = APIRouter(tags=["knowledge-gaps"])


class KnowledgeGapResponse(BaseModel):
    """One aggregated gap topic: a representative question and its demand signal."""

    question: str
    ask_count: int
    user_count: int
    last_asked_at: datetime


@router.get("/repositories/{repository_id}/knowledge-gaps")
async def list_knowledge_gaps(
    repository_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[KnowledgeGapResponse]:
    """Ranked knowledge gaps for a repository (admin-only): questions the vault could
    not answer, aggregated case/whitespace-insensitively, most-asked first."""
    if await session.get(Repository, repository_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    gaps = await gap_service.list_knowledge_gaps(session, repository_id, limit=limit)
    return [
        KnowledgeGapResponse(
            question=g.question,
            ask_count=g.ask_count,
            user_count=g.user_count,
            last_asked_at=g.last_asked_at,
        )
        for g in gaps
    ]


class RejectGapRequest(BaseModel):
    """An admin's decision that a gap question is out of scope, with why."""

    question: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class GapRejectionResponse(BaseModel):
    """A rejected gap: what was asked, why it was rejected, by whom, and when."""

    question: str
    reason: str
    rejected_by: str | None
    rejected_at: datetime


@router.post(
    "/repositories/{repository_id}/knowledge-gaps/reject", status_code=status.HTTP_201_CREATED
)
async def reject_knowledge_gap(
    repository_id: uuid.UUID,
    payload: RejectGapRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> GapRejectionResponse:
    """Reject a knowledge gap with a required reason (admin-only)."""
    if await session.get(Repository, repository_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    rejection = await gap_service.reject_gap(
        session, repository_id, question=payload.question, reason=payload.reason, admin_id=admin.id
    )
    await session.commit()
    return GapRejectionResponse(
        question=rejection.question,
        reason=rejection.reason,
        rejected_by=admin.username,
        rejected_at=rejection.created_at,
    )


@router.get("/repositories/{repository_id}/knowledge-gaps/rejected")
async def list_rejected_knowledge_gaps(
    repository_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[GapRejectionResponse]:
    """Rejected knowledge gaps for a repository, newest first (admin-only)."""
    if await session.get(Repository, repository_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    rejections = await gap_service.list_rejected_gaps(session, repository_id)
    author_ids = {r.rejected_by for r in rejections if r.rejected_by}
    authors: dict[uuid.UUID, str] = {}
    if author_ids:
        rows = (await session.execute(select(User).where(User.id.in_(author_ids)))).scalars().all()
        authors = {u.id: u.username for u in rows}
    return [
        GapRejectionResponse(
            question=r.question,
            reason=r.reason,
            rejected_by=authors.get(r.rejected_by) if r.rejected_by else None,
            rejected_at=r.created_at,
        )
        for r in rejections
    ]
