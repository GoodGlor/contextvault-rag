"""Grant model — a user's access to a repository (many-to-many, time-boxable)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Grant(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Grants one user read access to one repository, optionally until an expiry."""

    __tablename__ = "grants"
    __table_args__ = (UniqueConstraint("user_id", "repository_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Null means the grant does not expire.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
