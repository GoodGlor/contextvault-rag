"""Admin access-grant endpoints.

An admin grants a user read access to a repository, optionally time-boxed, and
revokes it. Grants are the access model's core: every retrieval is hard-filtered to
the caller's *active* grants (``retrieval.search`` / the query endpoint). The
user-facing "repositories I can reach" listing lives on the repositories router;
here we own the admin management surface plus the per-repo grant list.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.db.session import get_session
from contextvault.models import Grant, Repository, User
from contextvault.services import grants as grant_service
from contextvault.services import users as user_service

router = APIRouter(tags=["grants"])


class GrantRequest(BaseModel):
    """Admin grant payload: which user, and an optional expiry (null = never)."""

    user_id: uuid.UUID
    expires_at: datetime | None = None


class GrantResponse(BaseModel):
    """A grant of one user's access to one repository."""

    id: uuid.UUID
    user_id: uuid.UUID
    repository_id: uuid.UUID
    expires_at: datetime | None

    model_config = {"from_attributes": True}


async def _require_repo(session: AsyncSession, repository_id: uuid.UUID) -> Repository:
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    return repo


@router.post("/repositories/{repository_id}/grants", status_code=status.HTTP_200_OK)
async def grant_access(
    repository_id: uuid.UUID,
    payload: GrantRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> GrantResponse:
    """Grant a user access to a repository (idempotent — re-granting sets the expiry)."""
    await _require_repo(session, repository_id)
    if await user_service.get_user_by_id(session, payload.user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    grant = await grant_service.grant_access(
        session,
        user_id=payload.user_id,
        repository_id=repository_id,
        expires_at=payload.expires_at,
    )
    await session.commit()
    return GrantResponse.model_validate(grant)


@router.delete(
    "/repositories/{repository_id}/grants/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_access(
    repository_id: uuid.UUID,
    user_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke a user's access to a repository. 404 if no such grant exists."""
    removed = await grant_service.revoke_access(
        session, user_id=user_id, repository_id=repository_id
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/repositories/{repository_id}/grants")
async def list_grants(
    repository_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[GrantResponse]:
    """List every grant on a repository (including expired ones — the admin view)."""
    await _require_repo(session, repository_id)
    grants: list[Grant] = list(
        await grant_service.list_grants_for_repository(session, repository_id)
    )
    return [GrantResponse.model_validate(g) for g in grants]
