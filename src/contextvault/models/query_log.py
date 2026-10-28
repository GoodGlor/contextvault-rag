"""QueryLog model — one row per user question.

Every query is logged: it is the raw material for the knowledge-gap dashboard
(#31) and usage analytics (#33). The row captures who asked (nullable — see below),
against which repository, the question text, the retrieval signal (best similarity
and how many chunks cleared the relevance threshold), whether the answer was
grounded, and — via ``created_at`` — when.

``user_id`` is ``ON DELETE SET NULL`` (nullable): deleting a user (#28) anonymizes
their past questions to "asked by a deleted user" instead of erasing them, so the
analytics signal survives the account. ``repository_id`` cascades:
a repository's history dies with the repository.
"""

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class QueryLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single logged query and its retrieval outcome."""

    __tablename__ = "query_logs"

    # Null once the asker is deleted — the question is anonymized, not removed.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # Best similarity among all retrievable chunks (null when nothing was
    # retrievable at all — empty/inaccessible vault). Feeds the gap signal.
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How many chunks cleared the relevance threshold and grounded the answer.
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # True when the answer was the honest "not in this vault" — a knowledge gap.
    not_in_vault: Mapped[bool] = mapped_column(Boolean, nullable=False)
