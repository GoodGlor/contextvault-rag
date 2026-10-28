"""Knowledge-gap dashboard tests.

A gap is a logged query the vault could not answer (``not_in_vault = True``). The
admin dashboard aggregates similar gap questions (case/whitespace-insensitive), ranks
them by demand, and scopes them per repository. These tests write ``query_logs`` rows
directly (the logging path itself is covered by ``test_query_logging``) and assert:

1. only ``not_in_vault`` queries surface (answered ones never do);
2. similar phrasings aggregate into one ranked topic with an ask/user count;
3. results are ranked most-asked first and scoped to the repository;
4. the endpoint is admin-only and 404s for an unknown repository.
"""

import uuid
from collections.abc import AsyncGenerator

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
    question: str,
    not_in_vault: bool,
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


# --------------------------------------------------------------------------- #
# Only unanswered questions surface, aggregated and ranked
# --------------------------------------------------------------------------- #


async def test_only_unanswered_questions_are_gaps(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Handbook")
    await _log(
        db_session, repo_id=repo.id, question="What is the refund policy?", not_in_vault=True
    )
    await _log(db_session, repo_id=repo.id, question="Answered fine", not_in_vault=False)

    resp = await client.get(
        f"/repositories/{repo.id}/knowledge-gaps", headers=_auth(await _token(client, "admin"))
    )
    assert resp.status_code == 200
    gaps = resp.json()
    assert len(gaps) == 1
    assert gaps[0]["question"] == "What is the refund policy?"
    assert gaps[0]["ask_count"] == 1


async def test_similar_questions_aggregate_with_counts(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    repo = await _repo(db_session, "Handbook")
    # Three near-identical phrasings (case/whitespace) collapse into one topic.
    await _log(
        db_session,
        repo_id=repo.id,
        question="What is the VPN?",
        not_in_vault=True,
        user_id=alice.id,
    )
    await _log(
        db_session, repo_id=repo.id, question="what is the vpn?", not_in_vault=True, user_id=bob.id
    )
    await _log(
        db_session,
        repo_id=repo.id,
        question="What  is   the  VPN?",
        not_in_vault=True,
        user_id=alice.id,
    )

    resp = await client.get(
        f"/repositories/{repo.id}/knowledge-gaps", headers=_auth(await _token(client, "admin"))
    )
    gaps = resp.json()
    assert len(gaps) == 1
    assert gaps[0]["ask_count"] == 3
    assert gaps[0]["user_count"] == 2  # alice + bob (alice asked twice)


async def test_gaps_are_ranked_by_demand(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Handbook")
    for _ in range(3):
        await _log(db_session, repo_id=repo.id, question="Popular gap?", not_in_vault=True)
    await _log(db_session, repo_id=repo.id, question="Rare gap?", not_in_vault=True)

    resp = await client.get(
        f"/repositories/{repo.id}/knowledge-gaps", headers=_auth(await _token(client, "admin"))
    )
    gaps = resp.json()
    assert [g["question"] for g in gaps] == ["Popular gap?", "Rare gap?"]
    assert [g["ask_count"] for g in gaps] == [3, 1]


async def test_gaps_are_scoped_to_repository(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo_a = await _repo(db_session, "A")
    repo_b = await _repo(db_session, "B")
    await _log(db_session, repo_id=repo_a.id, question="A-only gap?", not_in_vault=True)
    await _log(db_session, repo_id=repo_b.id, question="B-only gap?", not_in_vault=True)

    resp = await client.get(
        f"/repositories/{repo_a.id}/knowledge-gaps", headers=_auth(await _token(client, "admin"))
    )
    assert [g["question"] for g in resp.json()] == ["A-only gap?"]


async def test_no_gaps_returns_empty(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Handbook")
    resp = await client.get(
        f"/repositories/{repo.id}/knowledge-gaps", headers=_auth(await _token(client, "admin"))
    )
    assert resp.status_code == 200
    assert resp.json() == []


# --------------------------------------------------------------------------- #
# Authorization + not-found
# --------------------------------------------------------------------------- #


async def test_gaps_require_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "regular")
    repo = await _repo(db_session, "Handbook")
    resp = await client.get(
        f"/repositories/{repo.id}/knowledge-gaps", headers=_auth(await _token(client, "regular"))
    )
    assert resp.status_code == 403


async def test_gaps_require_authentication(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session, "Handbook")
    resp = await client.get(f"/repositories/{repo.id}/knowledge-gaps")
    assert resp.status_code == 401


async def test_gaps_unknown_repository_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    resp = await client.get(
        f"/repositories/{uuid.uuid4()}/knowledge-gaps",
        headers=_auth(await _token(client, "admin")),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Reject a gap (admin-only) + rejected-list
# --------------------------------------------------------------------------- #


async def test_reject_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "alice")
    repo = await _repo(db_session, "Handbook")
    resp = await client.post(
        f"/repositories/{repo.id}/knowledge-gaps/reject",
        json={"question": "Q", "reason": "r"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 403


async def test_reject_empty_reason_is_422(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Handbook")
    resp = await client.post(
        f"/repositories/{repo.id}/knowledge-gaps/reject",
        json={"question": "Q", "reason": ""},
        headers=_auth(await _token(client, "admin")),
    )
    assert resp.status_code == 422


async def test_reject_then_gap_hidden_and_listed_rejected(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Handbook")
    await _log(db_session, repo_id=repo.id, question="What is the VPN?", not_in_vault=True)
    token = await _token(client, "admin")
    r = await client.post(
        f"/repositories/{repo.id}/knowledge-gaps/reject",
        json={"question": "What is the VPN?", "reason": "Out of scope"},
        headers=_auth(token),
    )
    assert r.status_code == 201
    gaps = (
        await client.get(f"/repositories/{repo.id}/knowledge-gaps", headers=_auth(token))
    ).json()
    assert gaps == []
    rejected = (
        await client.get(f"/repositories/{repo.id}/knowledge-gaps/rejected", headers=_auth(token))
    ).json()
    assert rejected[0]["question"] == "What is the VPN?"
    assert rejected[0]["reason"] == "Out of scope"
    assert rejected[0]["rejected_by"] == "admin"
