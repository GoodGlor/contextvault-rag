"""Integration tests for admin repository rename & delete.

Completes repository management: an admin can update a repo's name/description
(`PATCH`) and delete it (`DELETE`, confirmation-gated by echoing the name), with
its sources / chunks / grants cascading away. Mirrors the real-auth httpx harness.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Repository, Role, Source, SourceKind
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


async def _token(client: AsyncClient, db_session: AsyncSession, role: Role) -> str:
    username = f"{role.value}user"
    await user_service.create_user(db_session, username=username, password="pw", role=role)
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _repo(db_session: AsyncSession, name: str = "Handbook") -> Repository:
    repo = Repository(name=name, description="old")
    db_session.add(repo)
    await db_session.flush()
    return repo


# --- rename / edit -----------------------------------------------------------


async def test_patch_requires_authentication(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    resp = await client.patch(f"/repositories/{repo.id}", json={"name": "New"})
    assert resp.status_code == 401


async def test_patch_forbidden_for_non_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.USER)
    resp = await client.patch(
        f"/repositories/{repo.id}", json={"name": "New"}, headers=_auth(token)
    )
    assert resp.status_code == 403


async def test_admin_renames_repository(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.patch(
        f"/repositories/{repo.id}",
        json={"name": "Renamed", "description": "new"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Renamed"
    assert body["description"] == "new"
    await db_session.refresh(repo)
    assert repo.name == "Renamed" and repo.description == "new"


async def test_patch_leaves_omitted_fields_unchanged(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.patch(
        f"/repositories/{repo.id}",
        json={"description": "just the description"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    await db_session.refresh(repo)
    assert repo.name == "Handbook"  # untouched
    assert repo.description == "just the description"


async def test_patch_can_clear_description(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.patch(
        f"/repositories/{repo.id}", json={"description": None}, headers=_auth(token)
    )
    assert resp.status_code == 200
    await db_session.refresh(repo)
    assert repo.description is None


async def test_patch_rejects_blank_name(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.patch(f"/repositories/{repo.id}", json={"name": ""}, headers=_auth(token))
    assert resp.status_code == 422


async def test_patch_unknown_repository_404(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.patch(
        f"/repositories/{uuid.uuid4()}", json={"name": "X"}, headers=_auth(token)
    )
    assert resp.status_code == 404


# --- delete ------------------------------------------------------------------


async def test_delete_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.USER)
    resp = await client.request(
        "DELETE",
        f"/repositories/{repo.id}",
        json={"confirm_name": "Handbook"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_delete_requires_name_confirmation(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.request(
        "DELETE", f"/repositories/{repo.id}", json={"confirm_name": "wrong"}, headers=_auth(token)
    )
    assert resp.status_code == 400
    await db_session.refresh(repo)  # still there
    assert repo.name == "Handbook"


async def test_admin_deletes_repository_and_cascades(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    reader = await user_service.create_user(
        db_session, username="reader", password="pw", role=Role.USER
    )
    await grant_service.grant_access(
        db_session, user_id=reader.id, repository_id=repo.id, expires_at=None
    )
    db_session.add(Source(repository_id=repo.id, kind=SourceKind.DOCUMENT, title="doc.pdf"))
    await db_session.flush()
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.request(
        "DELETE",
        f"/repositories/{repo.id}",
        json={"confirm_name": "Handbook"},
        headers=_auth(token),
    )
    assert resp.status_code == 204
    assert await db_session.get(Repository, repo.id) is None
    # Its sources cascaded away with it.
    remaining = await db_session.execute(
        select(func.count()).select_from(Source).where(Source.repository_id == repo.id)
    )
    assert remaining.scalar_one() == 0


async def test_delete_unknown_repository_404(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.request(
        "DELETE", f"/repositories/{uuid.uuid4()}", json={"confirm_name": "x"}, headers=_auth(token)
    )
    assert resp.status_code == 404
