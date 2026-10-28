"""Conversation model — one saved chat thread per (user, repository).

The query page's conversation was previously client-only React state, lost on
reload. A ``Conversation`` persists it server-side, one per user per repository
(the ``Grant`` shape), so a reload restores the thread and the server — not the
client — is the authority on conversation history.
"""

import uuid

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user's running conversation with one repository."""

    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("user_id", "repository_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
