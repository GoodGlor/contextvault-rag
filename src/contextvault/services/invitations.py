"""Invitation persistence: issuing invites and redeeming them.

An admin issues an invitation for a new username; the invited user redeems it by
choosing their own password, which creates the account. The
service owns the two invariants the API relies on: an invite is single-use and
expiring, and the admin never handles the user's password.
"""

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.invite_tokens import generate_invite_token, hash_invite_token
from contextvault.models import Invitation, Role, User
from contextvault.services import users as user_service


class InvitationError(Exception):
    """Base class for invitation failures the API maps to HTTP responses."""


class UsernameTaken(InvitationError):
    """Raised when the target username already belongs to an account."""


class InvalidInvitation(InvitationError):
    """Raised when a token is unknown, expired, or already redeemed.

    Deliberately undifferentiated: the accept endpoint returns one uniform error
    for all three so a caller cannot probe which tokens exist or have expired.
    """


async def create_invitation(
    session: AsyncSession,
    *,
    username: str,
    role: Role = Role.USER,
    issued_by: uuid.UUID | None,
    expires_in: timedelta,
) -> tuple[Invitation, str]:
    """Issue an invitation for ``username`` and return it with its raw token.

    The raw token is returned once, for the invite link, and is never stored —
    only its hash is persisted. Rejects a username that already has an account so
    an invite can never shadow or hijack an existing user.
    """
    if await user_service.get_user_by_username(session, username) is not None:
        raise UsernameTaken(username)

    raw_token, token_hash = generate_invite_token()
    invitation = Invitation(
        token_hash=token_hash,
        username=username,
        role=role,
        expires_at=datetime.now(UTC) + expires_in,
        created_by_id=issued_by,
    )
    session.add(invitation)
    await session.flush()
    return invitation, raw_token


async def accept_invitation(session: AsyncSession, *, token: str, password: str) -> User:
    """Redeem a token: create the account with the chosen password, spend the invite.

    Validates the invitation is known, unexpired, and unredeemed, then creates the
    user (activated, with their own password — ``must_change_password`` stays
    false, unlike the temp-password recovery flow) and marks the invite accepted
    so it can never be reused. Raises :class:`InvalidInvitation` for any invalid
    token and :class:`UsernameTaken` if the username was claimed since issuance.

    The invitation row is locked ``FOR UPDATE`` so single-use is enforced *by
    design*, not incidentally: two concurrent redemptions of the same token
    serialize, and the loser sees ``accepted_at`` already set and is rejected —
    rather than both racing past the check into a duplicate-account error.
    """
    result = await session.execute(
        sa.select(Invitation)
        .where(Invitation.token_hash == hash_invite_token(token))
        .with_for_update()
    )
    invitation = result.scalar_one_or_none()
    if invitation is None or invitation.accepted_at is not None:
        raise InvalidInvitation
    if invitation.expires_at <= datetime.now(UTC):
        raise InvalidInvitation

    if await user_service.get_user_by_username(session, invitation.username) is not None:
        raise UsernameTaken(invitation.username)

    user = await user_service.create_user(
        session,
        username=invitation.username,
        password=password,
        role=invitation.role,
    )
    invitation.accepted_at = datetime.now(UTC)
    await session.flush()
    return user
