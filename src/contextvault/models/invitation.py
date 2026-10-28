"""Invitation model — a single-use, expiring onboarding token.

An admin issues an invitation for a new account (a username + role); the invited
user accepts it by choosing their own password, at which point the account is
created. The admin never sees or handles that password. The
token itself is stored only as a hash (see ``core/invite_tokens.py``); the raw
token lives solely in the invite link handed out once.

An invitation is *valid* while it is unexpired and unaccepted. ``accepted_at``
makes it single-use — once set, the same token can never be redeemed again.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.enums import Role
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Invitation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A pending onboarding invite: token hash, target username/role, expiry."""

    __tablename__ = "invitations"

    # SHA-256 hex digest of the raw token; the raw token is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    # The account the invite will create when accepted.
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=Role.USER,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Null until the invite is redeemed; once set, the invite is spent (single-use).
    accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # The admin who issued the invite, kept for audit. Detached (SET NULL) rather
    # than cascade-deleted if that admin is later removed, so the record survives.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
