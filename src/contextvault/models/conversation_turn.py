"""ConversationTurn model — one Q/A exchange in a saved conversation.

Each turn stores the question, the answer text, the honesty flag, and a JSONB
*snapshot* of the answer's citations and cited sources (the exact shapes the
query endpoint returns). Storing snapshots — not foreign keys — means a restored
answer renders identically even if a cited source is later edited or deleted.
"""

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ConversationTurn(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single exchange (question + grounded answer) within a conversation."""

    __tablename__ = "conversation_turns"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 0-based position within the conversation, oldest first.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    not_in_vault: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Snapshot of QueryResponse.citations / .sources at answer time (JSON-dumped).
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
