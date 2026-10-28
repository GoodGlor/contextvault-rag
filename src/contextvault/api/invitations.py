"""Onboarding invitation endpoints.

An admin issues a single-use, expiring invite for a new account; the invited user
redeems it on a public endpoint by choosing their own password, which creates the
account. The admin never sees or handles that password, and the raw token is
returned to the admin exactly once (only its hash is stored).
"""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.core.config import get_settings
from contextvault.db.session import get_session
from contextvault.models import Role, User
from contextvault.services import invitations as invitation_service

router = APIRouter(prefix="/invitations", tags=["invitations"])


class InvitationRequest(BaseModel):
    """Admin-supplied details for a new invite."""

    username: str = Field(min_length=1, max_length=255)
    role: Role = Role.USER
    # Optional per-invite expiry override; falls back to the configured default.
    expires_in_hours: int | None = Field(default=None, gt=0)


class InvitationResponse(BaseModel):
    """A freshly issued invite. ``token`` is shown once — it is never stored raw."""

    token: str
    username: str
    role: Role
    expires_at: datetime


class AcceptInvitationRequest(BaseModel):
    """A user redeeming an invite by choosing their own password."""

    token: str = Field(min_length=1)
    # The user picks their own password; a modest floor keeps it from being trivial.
    password: str = Field(min_length=8)


class AcceptedUserResponse(BaseModel):
    """The account created by accepting an invite."""

    id: uuid.UUID
    username: str
    role: Role


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: InvitationRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> InvitationResponse:
    """Issue an invite for a new account; returns the raw token once (admin-only)."""
    hours = payload.expires_in_hours or get_settings().invite_expiry_hours
    try:
        invitation, raw_token = await invitation_service.create_invitation(
            session,
            username=payload.username,
            role=payload.role,
            issued_by=admin.id,
            expires_in=timedelta(hours=hours),
        )
    except invitation_service.UsernameTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that username already exists",
        ) from exc
    await session.commit()
    return InvitationResponse(
        token=raw_token,
        username=invitation.username,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


@router.post("/accept", status_code=status.HTTP_201_CREATED)
async def accept_invitation(
    payload: AcceptInvitationRequest,
    session: AsyncSession = Depends(get_session),
) -> AcceptedUserResponse:
    """Redeem an invite: set the password, create the account (public endpoint)."""
    try:
        user = await invitation_service.accept_invitation(
            session, token=payload.token, password=payload.password
        )
    except invitation_service.InvalidInvitation as exc:
        # One uniform error for unknown / expired / already-used, so a caller
        # cannot probe which tokens exist.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired invitation",
        ) from exc
    except invitation_service.UsernameTaken as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that username already exists",
        ) from exc
    await session.commit()
    return AcceptedUserResponse(id=user.id, username=user.username, role=user.role)
