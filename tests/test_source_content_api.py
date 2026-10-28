"""Integration tests for the user-facing source-content endpoint.

A granted user can read a cited source's passage text
(`GET /repositories/{repo}/sources/{source}`); access is gated by the same
active-grant rule the query endpoint enforces (403 without), and a source that
isn't in the named repository is a 404. Mirrors the real-auth httpx harness.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Repository, Role, Source, SourceKind, SourceStatus, User
from contextvault.services import grants as grant_service
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


async def _login(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _reader(db_session: AsyncSession, username: str = "reader") -> User:
    return await user_service.create_user(
        db_session, username=username, password="pw", role=Role.USER
    )


async def _repo(db_session: AsyncSession, name: str = "Handbook") -> Repository:
    repo = Repository(name=name)
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _source(
    db_session: AsyncSession, repo: Repository, content: str = "the passage"
) -> Source:
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.DOCUMENT,
        title="policy.pdf",
        original_filename="policy.pdf",
        content=content,
        status=SourceStatus.DONE,
    )
    db_session.add(source)
    await db_session.flush()
    return source


async def test_requires_authentication(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    source = await _source(db_session, repo)
    resp = await client.get(f"/repositories/{repo.id}/sources/{source.id}")
    assert resp.status_code == 401


async def test_forbidden_without_grant(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    source = await _source(db_session, repo)
    await _reader(db_session)
    token = await _login(client, "reader")
    resp = await client.get(f"/repositories/{repo.id}/sources/{source.id}", headers=_auth(token))
    assert resp.status_code == 403


async def test_granted_user_reads_source_content(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    source = await _source(db_session, repo, content="Retention is 30 days.")
    reader = await _reader(db_session)
    await grant_service.grant_access(
        db_session, user_id=reader.id, repository_id=repo.id, expires_at=None
    )
    token = await _login(client, "reader")

    resp = await client.get(f"/repositories/{repo.id}/sources/{source.id}", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "policy.pdf"
    assert body["kind"] == "document"
    assert body["content"] == "Retention is 30 days."


async def test_source_in_another_repository_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session, "Handbook")
    other = await _repo(db_session, "Runbook")
    source = await _source(db_session, other)  # belongs to `other`, not `repo`
    reader = await _reader(db_session)
    await grant_service.grant_access(
        db_session, user_id=reader.id, repository_id=repo.id, expires_at=None
    )
    token = await _login(client, "reader")

    resp = await client.get(f"/repositories/{repo.id}/sources/{source.id}", headers=_auth(token))
    assert resp.status_code == 404


async def test_unknown_source_404(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    reader = await _reader(db_session)
    await grant_service.grant_access(
        db_session, user_id=reader.id, repository_id=repo.id, expires_at=None
    )
    token = await _login(client, "reader")
    resp = await client.get(f"/repositories/{repo.id}/sources/{uuid.uuid4()}", headers=_auth(token))
    assert resp.status_code == 404
