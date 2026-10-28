"""User-management endpoints (admin) — account recovery + deletion.

An admin can issue a random temporary password for a user who has lost access
 or permanently delete a user. The temp password is returned
to the admin **once**, in plaintext; only its hash is stored, and the user is
forced to replace it on next login (``must_change_password``), so the admin never
learns the user's real password. Deletion is confirmation-gated and preserves
analytics signal by anonymizing (detaching) the user's contributions rather than
erasing them.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.db.session import get_session
from contextvault.models import Role, User
from contextvault.services import users as user_service

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(BaseModel):
    """A user account as the admin management UI sees it (never the password hash)."""

    id: uuid.UUID
    username: str
    role: Role
    must_change_password: bool
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("")
async def list_users(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[UserResponse]:
    """List all user accounts, oldest first (admin-only)."""
    users = await user_service.list_users(session)
    return [UserResponse.model_validate(u) for u in users]


class ResetPasswordResponse(BaseModel):
    """A freshly issued temporary password (shown once) and the forced-change flag."""

    temporary_password: str
    must_change_password: bool


class DeleteUserRequest(BaseModel):
    """Confirmation payload: the client must echo the target's username to delete it."""

    confirm_username: str


@router.post("/{user_id}/reset-password", status_code=status.HTTP_200_OK)
async def reset_password(
    user_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ResetPasswordResponse:
    """Issue a random temporary password for a user; forces a change on next login."""
    user = await user_service.get_user_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    temporary = await user_service.reset_password(session, user)
    await session.commit()
    return ResetPasswordResponse(temporary_password=temporary, must_change_password=True)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    payload: DeleteUserRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Permanently delete a user, confirmation-gated by echoing their username.

    Grants cascade away; contributions (e.g. admin notes) detach rather than
    delete. The **last remaining admin** cannot be removed, so the
    system can never be locked out of its bootstrap invariant.
    """
    user = await user_service.get_user_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if payload.confirm_username != user.username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username confirmation does not match",
        )
    if user.role is Role.ADMIN and await user_service.count_admins(session) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete the last remaining admin",
        )
    await user_service.delete_user(session, user)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
