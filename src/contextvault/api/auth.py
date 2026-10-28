"""Authentication endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_authenticated_user
from contextvault.core.security import verify_password
from contextvault.core.tokens import create_access_token
from contextvault.db.session import get_session
from contextvault.models import User
from contextvault.services import users as user_service

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # If true the client must route the user to set a new password before use
    # (temp-password recovery flow).
    must_change_password: bool = False


@router.post("/login")
async def login(body: LoginRequest, session: AsyncSession = Depends(get_session)) -> TokenResponse:
    """Exchange username + password for a JWT access token."""
    user = await user_service.get_user_by_username(session, body.username)
    if user is None or not verify_password(body.password, user.password_hash):
        # Same response for unknown user and wrong password — don't leak which.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    token = create_access_token(subject=str(user.id), role=user.role.value)
    return TokenResponse(access_token=token, must_change_password=user.must_change_password)


class ChangePasswordRequest(BaseModel):
    current_password: str
    # A modest floor, matching the invite-accept policy.
    new_password: str = Field(min_length=8)


@router.post("/change-password", status_code=status.HTTP_200_OK)
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_authenticated_user),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Set a new password of the user's choosing, clearing any forced-change flag.

    Depends on ``get_authenticated_user`` (not ``get_current_user``) so a user
    under a forced-change bounce can still reach it — this is the escape hatch.
    Returns a fresh token whose ``must_change_password`` is now false.
    """
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    await user_service.change_password(session, user, new_password=body.new_password)
    await session.commit()
    token = create_access_token(subject=str(user.id), role=user.role.value)
    return TokenResponse(access_token=token, must_change_password=user.must_change_password)
