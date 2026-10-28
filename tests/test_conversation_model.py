"""Conversation + ConversationTurn model tests (persisted chat, per user+repo)."""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import Conversation, ConversationTurn, Repository, Role, User
from contextvault.services import users as user_service


async def _user(db_session: AsyncSession, name: str) -> User:
    return await user_service.create_user(db_session, username=name, password="pw", role=Role.USER)


async def _repo(db_session: AsyncSession, name: str) -> Repository:
    repo = Repository(name=name)
    db_session.add(repo)
    await db_session.flush()
    return repo


async def test_conversation_turn_round_trips(db_session: AsyncSession) -> None:
    user = await _user(db_session, "alice")
    repo = await _repo(db_session, "Handbook")
    convo = Conversation(user_id=user.id, repository_id=repo.id)
    db_session.add(convo)
    await db_session.flush()
    turn = ConversationTurn(
        conversation_id=convo.id,
        ordinal=0,
        question="What is the VPN?",
        answer="It is X [1].",
        not_in_vault=False,
        citations=[
            {
                "number": 1,
                "chunk_id": str(uuid.uuid4()),
                "source_id": str(uuid.uuid4()),
                "char_start": 0,
                "char_end": 5,
            }
        ],
        sources=[
            {
                "id": str(uuid.uuid4()),
                "title": "vpn.md",
                "original_filename": "vpn.md",
                "kind": "document",
                "verified": False,
                "author": None,
            }
        ],
    )
    db_session.add(turn)
    await db_session.flush()

    loaded = (
        await db_session.execute(
            sa.select(ConversationTurn).where(ConversationTurn.conversation_id == convo.id)
        )
    ).scalar_one()
    assert loaded.answer == "It is X [1]."
    assert loaded.citations[0]["number"] == 1
    assert loaded.sources[0]["title"] == "vpn.md"


async def test_one_conversation_per_user_and_repo(db_session: AsyncSession) -> None:
    user = await _user(db_session, "bob")
    repo = await _repo(db_session, "Handbook")
    db_session.add(Conversation(user_id=user.id, repository_id=repo.id))
    await db_session.flush()
    db_session.add(Conversation(user_id=user.id, repository_id=repo.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
