"""Integration tests for the saved-conversation endpoints (Task 4).

``GET /repositories/{id}/conversation`` restores this user's saved thread for a
repository (empty list, 200, when none exists yet); ``DELETE`` clears it. Both
mirror the ``/query`` endpoint's access guard: 404 for an unknown repository,
403 without an active grant — a user only ever sees their OWN conversation.

Fixtures (client / _token / _auth / _grant) are copied from test_query_api.py so
the grant-seeding matches the pattern the query endpoint's own tests rely on.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Conversation, Repository, Role, User
from contextvault.services import conversations as convo_service
from contextvault.services import users as user_service


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _user(db_session: AsyncSession, role: Role, username: str) -> User:
    return await user_service.create_user(db_session, username=username, password="pw", role=role)


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


async def _repo(db_session: AsyncSession) -> Repository:
    repo = Repository(name="Vault")
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _grant(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    repository_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
) -> None:
    from contextvault.models import Grant

    db_session.add(Grant(user_id=user_id, repository_id=repository_id, expires_at=expires_at))
    await db_session.flush()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_turn(
    db_session: AsyncSession, user_id: uuid.UUID, repository_id: uuid.UUID
) -> None:
    conversation = await convo_service.get_or_create_conversation(
        db_session, user_id, repository_id
    )
    await convo_service.append_turn(
        db_session,
        conversation.id,
        question="q0",
        answer="a0",
        not_in_vault=False,
        citations=[
            {
                "number": 1,
                "chunk_id": str(uuid.uuid4()),
                "source_id": str(uuid.uuid4()),
                "char_start": 0,
                "char_end": 10,
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
    await db_session.commit()


async def test_get_returns_saved_turns_for_owner(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    await _seed_turn(db_session, user.id, repo.id)

    resp = await client.get(
        f"/repositories/{repo.id}/conversation", headers=_auth(await _token(client, "alice"))
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["turns"][0]["question"] == "q0"
    assert body["turns"][0]["answer"] == "a0"
    assert body["turns"][0]["not_in_vault"] is False
    assert body["turns"][0]["sources"][0]["title"] == "vpn.md"
    assert body["turns"][0]["citations"][0]["number"] == 1


async def test_get_empty_when_no_conversation(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, user.id, repo.id)

    resp = await client.get(
        f"/repositories/{repo.id}/conversation", headers=_auth(await _token(client, "bob"))
    )
    assert resp.status_code == 200
    assert resp.json() == {"turns": []}


async def test_get_requires_active_grant(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.USER, "mallory")

    resp = await client.get(
        f"/repositories/{repo.id}/conversation", headers=_auth(await _token(client, "mallory"))
    )
    assert resp.status_code == 403


async def test_delete_clears_conversation(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "carol")
    await _grant(db_session, user.id, repo.id)
    await _seed_turn(db_session, user.id, repo.id)

    resp = await client.delete(
        f"/repositories/{repo.id}/conversation", headers=_auth(await _token(client, "carol"))
    )
    assert resp.status_code == 204
    assert (
        await db_session.execute(sa.select(sa.func.count()).select_from(Conversation))
    ).scalar_one() == 0


async def test_get_404_unknown_repo(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "dave")
    resp = await client.get(
        f"/repositories/{uuid.uuid4()}/conversation", headers=_auth(await _token(client, "dave"))
    )
    assert resp.status_code == 404


async def test_delete_requires_active_grant(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.USER, "eve")

    resp = await client.delete(
        f"/repositories/{repo.id}/conversation", headers=_auth(await _token(client, "eve"))
    )
    assert resp.status_code == 403


async def test_delete_404_unknown_repo(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "frank")
    resp = await client.delete(
        f"/repositories/{uuid.uuid4()}/conversation", headers=_auth(await _token(client, "frank"))
    )
    assert resp.status_code == 404


async def test_get_only_returns_own_conversation(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # Two users each granted the same repo; a saved turn for one must not leak to
    # the other — GET is scoped to the caller's own conversation.
    repo = await _repo(db_session)
    owner = await _user(db_session, Role.USER, "grace")
    other = await _user(db_session, Role.USER, "heidi")
    await _grant(db_session, owner.id, repo.id)
    await _grant(db_session, other.id, repo.id)
    await _seed_turn(db_session, owner.id, repo.id)

    resp = await client.get(
        f"/repositories/{repo.id}/conversation", headers=_auth(await _token(client, "heidi"))
    )
    assert resp.status_code == 200
    assert resp.json() == {"turns": []}
