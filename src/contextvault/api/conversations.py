"""Saved-conversation endpoints (persisted chat, per user+repo).

``GET`` restores this user's thread for a repository so a page reload rebuilds the
conversation exactly (each turn carries its citation/source snapshot). ``DELETE``
is the "Clear conversation" action. Both require an active grant, like ``/query``.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_current_user
from contextvault.api.query import CitationResponse, SourceReferenceResponse
from contextvault.db.session import get_session
from contextvault.models import Repository, User
from contextvault.services import conversations as convo_service
from contextvault.services import grants as grant_service

router = APIRouter(tags=["conversation"])


class ConversationTurnResponse(BaseModel):
    question: str
    answer: str
    not_in_vault: bool
    citations: list[CitationResponse]
    sources: list[SourceReferenceResponse]


class ConversationResponse(BaseModel):
    turns: list[ConversationTurnResponse]


async def _guard(session: AsyncSession, user: User, repository_id: uuid.UUID) -> None:
    if await session.get(Repository, repository_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    if not await grant_service.has_active_grant(session, user.id, repository_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )


@router.get("/repositories/{repository_id}/conversation")
async def get_conversation(
    repository_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ConversationResponse:
    """This user's saved conversation for the repository (empty when none yet)."""
    await _guard(session, user, repository_id)
    conversation = await convo_service.get_or_create_conversation(session, user.id, repository_id)
    turns = await convo_service.list_turns(session, conversation.id)
    await session.commit()
    return ConversationResponse(
        turns=[
            ConversationTurnResponse(
                question=t.question,
                answer=t.answer,
                not_in_vault=t.not_in_vault,
                citations=[CitationResponse.model_validate(c) for c in t.citations],
                sources=[SourceReferenceResponse.model_validate(s) for s in t.sources],
            )
            for t in turns
        ]
    )


@router.delete("/repositories/{repository_id}/conversation", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    repository_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete this user's saved conversation for the repository."""
    await _guard(session, user, repository_id)
    await convo_service.clear_conversation(session, user.id, repository_id)
    await session.commit()
