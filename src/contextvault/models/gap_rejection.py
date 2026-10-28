"""GapRejection model — an admin's decision to reject a knowledge gap.

A knowledge gap is an aggregated question (grouped case/whitespace-insensitively)
the vault could not answer. Besides *answering* a gap (an Admin Note), an admin can
*reject* it — decide it won't be covered — with a required written reason. A
rejection is keyed by ``(repository_id, normalized_question)`` (matching the gap
aggregation) and excludes that question from the active gap list.
"""

import uuid

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class GapRejection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One rejected knowledge-gap topic for a repository, with the admin's reason."""

    __tablename__ = "gap_rejections"
    __table_args__ = (UniqueConstraint("repository_id", "normalized_question"),)

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The gap identity: the same normalization used by list_knowledge_gaps.
    normalized_question: Mapped[str] = mapped_column(Text, nullable=False)
    # A representative original phrasing, for display in the rejected list.
    question: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # Null once the admin is deleted — the decision survives the account.
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
