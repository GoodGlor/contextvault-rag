"""Conversation persistence service (saved chat, per user+repo).

One ``Conversation`` per (user, repository); turns are appended oldest-first with
a monotonic ``ordinal``. ``recent_history`` returns the most recent tail as
``(question, answer)`` pairs for the LLM prompt — the server, not the client, owns
history. All functions add/flush; the caller commits.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import Conversation, ConversationTurn


async def get_or_create_conversation(
    session: AsyncSession, user_id: uuid.UUID, repository_id: uuid.UUID
) -> Conversation:
    """Return this user's conversation for the repository, creating it if absent."""
    existing = (
        await session.execute(
            sa.select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.repository_id == repository_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    convo = Conversation(user_id=user_id, repository_id=repository_id)
    session.add(convo)
    await session.flush()
    return convo


async def list_turns(session: AsyncSession, conversation_id: uuid.UUID) -> list[ConversationTurn]:
    """All turns of a conversation, oldest first."""
    rows = (
        (
            await session.execute(
                sa.select(ConversationTurn)
                .where(ConversationTurn.conversation_id == conversation_id)
                .order_by(ConversationTurn.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def recent_history(
    session: AsyncSession, conversation_id: uuid.UUID, limit: int
) -> list[tuple[str, str]]:
    """The last ``limit`` ``(question, answer)`` pairs, oldest first, for prompting."""
    rows = (
        await session.execute(
            sa.select(ConversationTurn.question, ConversationTurn.answer)
            .where(ConversationTurn.conversation_id == conversation_id)
            .order_by(ConversationTurn.ordinal.desc())
            .limit(limit)
        )
    ).all()
    return [(r.question, r.answer) for r in reversed(rows)]


async def append_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    question: str,
    answer: str,
    not_in_vault: bool,
    citations: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> ConversationTurn:
    """Append one exchange; ``ordinal`` continues after the current last turn."""
    next_ordinal = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.max(ConversationTurn.ordinal), -1) + 1).where(
                ConversationTurn.conversation_id == conversation_id
            )
        )
    ).scalar_one()
    turn = ConversationTurn(
        conversation_id=conversation_id,
        ordinal=next_ordinal,
        question=question,
        answer=answer,
        not_in_vault=not_in_vault,
        citations=citations,
        sources=sources,
    )
    session.add(turn)
    await session.flush()
    return turn


async def clear_conversation(
    session: AsyncSession, user_id: uuid.UUID, repository_id: uuid.UUID
) -> None:
    """Delete this user's conversation for the repository (turns cascade)."""
    await session.execute(
        sa.delete(Conversation).where(
            Conversation.user_id == user_id,
            Conversation.repository_id == repository_id,
        )
    )
    await session.flush()
