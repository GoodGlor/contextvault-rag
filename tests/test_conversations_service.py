"""Conversation service tests: get-or-create, append, history tail, clear."""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import Conversation, ConversationTurn, Repository, Role, User
from contextvault.services import conversations as convo_service
from contextvault.services import users as user_service


async def _seed(db_session: AsyncSession) -> tuple[User, Repository]:
    user = await user_service.create_user(
        db_session, username="alice", password="pw", role=Role.USER
    )
    repo = Repository(name="Handbook")
    db_session.add(repo)
    await db_session.flush()
    return user, repo


async def test_get_or_create_is_idempotent(db_session: AsyncSession) -> None:
    user, repo = await _seed(db_session)
    a = await convo_service.get_or_create_conversation(db_session, user.id, repo.id)
    b = await convo_service.get_or_create_conversation(db_session, user.id, repo.id)
    assert a.id == b.id


async def test_append_increments_ordinal_and_history_tail(db_session: AsyncSession) -> None:
    user, repo = await _seed(db_session)
    convo = await convo_service.get_or_create_conversation(db_session, user.id, repo.id)
    for i in range(3):
        await convo_service.append_turn(
            db_session,
            convo.id,
            question=f"q{i}",
            answer=f"a{i}",
            not_in_vault=False,
            citations=[],
            sources=[],
        )
    turns = await convo_service.list_turns(db_session, convo.id)
    assert [t.ordinal for t in turns] == [0, 1, 2]
    hist = await convo_service.recent_history(db_session, convo.id, limit=2)
    assert hist == [("q1", "a1"), ("q2", "a2")]


async def test_clear_removes_conversation_and_turns(db_session: AsyncSession) -> None:
    user, repo = await _seed(db_session)
    convo = await convo_service.get_or_create_conversation(db_session, user.id, repo.id)
    await convo_service.append_turn(
        db_session,
        convo.id,
        question="q",
        answer="a",
        not_in_vault=False,
        citations=[],
        sources=[],
    )
    await convo_service.clear_conversation(db_session, user.id, repo.id)
    convo_count = await db_session.execute(sa.select(sa.func.count()).select_from(Conversation))
    assert convo_count.scalar_one() == 0
    turn_count = await db_session.execute(sa.select(sa.func.count()).select_from(ConversationTurn))
    assert turn_count.scalar_one() == 0
