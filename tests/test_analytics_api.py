"""Query-analytics tests.

``GET /analytics`` (admin-only) aggregates the query log (#30) into the usage
insight a dashboard needs: totals + answered/gap rate, per-repository volume,
most-asked questions, most-active users, and the answered-vs-gap rate over time.
Tests write ``query_logs`` rows directly and assert each aggregate.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import QueryLog, Repository, Role, User
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


async def _repo(db_session: AsyncSession, name: str) -> Repository:
    repo = Repository(name=name)
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _log(
    db_session: AsyncSession,
    *,
    repo_id: uuid.UUID,
    question: str = "A question?",
    not_in_vault: bool = False,
    user_id: uuid.UUID | None = None,
) -> None:
    db_session.add(
        QueryLog(
            user_id=user_id,
            repository_id=repo_id,
            question=question,
            top_score=None if not_in_vault else 0.9,
            chunk_count=0 if not_in_vault else 2,
            not_in_vault=not_in_vault,
        )
    )
    await db_session.flush()


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _analytics(client: AsyncClient, token: str) -> dict[str, Any]:
    resp = await client.get("/analytics", headers=_auth(token))
    assert resp.status_code == 200
    data: dict[str, Any] = resp.json()
    return data


# --------------------------------------------------------------------------- #
# Totals + answered/gap rate
# --------------------------------------------------------------------------- #


async def test_totals_and_gap_rate(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Repo")
    await _log(db_session, repo_id=repo.id, not_in_vault=False)
    await _log(db_session, repo_id=repo.id, not_in_vault=False)
    await _log(db_session, repo_id=repo.id, not_in_vault=True)

    data = await _analytics(client, await _token(client, "admin"))
    assert data["total_queries"] == 3
    assert data["answered"] == 2
    assert data["not_in_vault"] == 1
    assert data["not_in_vault_rate"] == pytest.approx(1 / 3)


async def test_empty_analytics_is_zeroed(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    data = await _analytics(client, await _token(client, "admin"))
    assert data["total_queries"] == 0
    assert data["not_in_vault_rate"] == 0.0
    assert data["per_repository"] == []
    assert data["top_questions"] == []
    assert data["active_users"] == []
    assert data["by_day"] == []


# --------------------------------------------------------------------------- #
# Per-repository volume
# --------------------------------------------------------------------------- #


async def test_per_repository_volume(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    busy = await _repo(db_session, "Busy")
    quiet = await _repo(db_session, "Quiet")
    for _ in range(3):
        await _log(db_session, repo_id=busy.id)
    await _log(db_session, repo_id=busy.id, not_in_vault=True)
    await _log(db_session, repo_id=quiet.id)

    data = await _analytics(client, await _token(client, "admin"))
    per_repo = data["per_repository"]
    assert [r["repository_name"] for r in per_repo] == ["Busy", "Quiet"]  # busiest first
    assert per_repo[0]["query_count"] == 4
    assert per_repo[0]["not_in_vault_count"] == 1
    assert per_repo[1]["query_count"] == 1


# --------------------------------------------------------------------------- #
# Most-asked questions (aggregated case/whitespace-insensitively)
# --------------------------------------------------------------------------- #


async def test_top_questions_aggregate_and_rank(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Repo")
    for q in ("What is the VPN?", "what  is the vpn?", "WHAT IS THE VPN?"):
        await _log(db_session, repo_id=repo.id, question=q)
    await _log(db_session, repo_id=repo.id, question="Rare?")

    data = await _analytics(client, await _token(client, "admin"))
    top = data["top_questions"]
    assert top[0]["ask_count"] == 3  # the three VPN phrasings collapse
    assert [t["ask_count"] for t in top] == [3, 1]


async def test_top_limit_bounds_lists(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Repo")
    for i in range(5):
        await _log(db_session, repo_id=repo.id, question=f"Question {i}?")

    resp = await client.get(
        "/analytics", params={"top_limit": 2}, headers=_auth(await _token(client, "admin"))
    )
    assert resp.status_code == 200
    assert len(resp.json()["top_questions"]) == 2


# --------------------------------------------------------------------------- #
# Most-active users — known users only (anonymized excluded)
# --------------------------------------------------------------------------- #


async def test_active_users_rank_and_exclude_anonymized(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    repo = await _repo(db_session, "Repo")
    for _ in range(3):
        await _log(db_session, repo_id=repo.id, user_id=alice.id)
    await _log(db_session, repo_id=repo.id, user_id=bob.id)
    await _log(db_session, repo_id=repo.id, user_id=None)  # anonymized — excluded

    data = await _analytics(client, await _token(client, "admin"))
    active = data["active_users"]
    assert [u["username"] for u in active] == ["alice", "bob"]
    assert active[0]["query_count"] == 3


# --------------------------------------------------------------------------- #
# Answered-vs-gap over time
# --------------------------------------------------------------------------- #


async def test_by_day_series(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Repo")
    await _log(db_session, repo_id=repo.id, not_in_vault=False)
    await _log(db_session, repo_id=repo.id, not_in_vault=True)

    data = await _analytics(client, await _token(client, "admin"))
    # All rows share "today" (created_at server-default now()), so one bucket.
    assert len(data["by_day"]) == 1
    point = data["by_day"][0]
    assert point["total"] == 2
    assert point["not_in_vault"] == 1
    assert point["day"]  # an ISO date string


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


async def test_analytics_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "regular")
    resp = await client.get("/analytics", headers=_auth(await _token(client, "regular")))
    assert resp.status_code == 403


async def test_analytics_requires_authentication(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.get("/analytics")
    assert resp.status_code == 401
