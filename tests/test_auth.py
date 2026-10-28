"""Integration tests for login, the auth dependency, and role-based access.

Driven with httpx.AsyncClient (not Starlette's TestClient) so the app runs in
the same event loop as the async ``db_session`` fixture — asyncpg connections are
bound to the loop that created them.
"""

from collections.abc import AsyncGenerator

import pytest
from fastapi import Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_current_user, require_admin
from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Role, User
from contextvault.services import users as user_service


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session

    # Routes that exercise the auth/RBAC dependencies.
    @app.get("/_test/me")
    async def _me(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"username": user.username, "role": user.role.value}

    @app.get("/_test/admin")
    async def _admin(_: User = Depends(require_admin)) -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _seed(db_session: AsyncSession) -> None:
    await user_service.create_user(
        db_session, username="admin", password="adminpw", role=Role.ADMIN
    )
    await user_service.create_user(db_session, username="user", password="userpw", role=Role.USER)


async def test_login_returns_token(db_session: AsyncSession, client: AsyncClient) -> None:
    await _seed(db_session)
    resp = await client.post("/auth/login", json={"username": "admin", "password": "adminpw"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_rejects_wrong_password(db_session: AsyncSession, client: AsyncClient) -> None:
    await _seed(db_session)
    resp = await client.post("/auth/login", json={"username": "admin", "password": "nope"})
    assert resp.status_code == 401


async def test_login_rejects_unknown_user(db_session: AsyncSession, client: AsyncClient) -> None:
    resp = await client.post("/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401


async def test_login_surfaces_must_change_password(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await user_service.create_user(
        db_session, username="temp", password="temp", role=Role.USER, must_change_password=True
    )
    resp = await client.post("/auth/login", json={"username": "temp", "password": "temp"})
    assert resp.json()["must_change_password"] is True


async def test_me_requires_authentication(client: AsyncClient) -> None:
    resp = await client.get("/_test/me")
    assert resp.status_code == 401


async def test_me_rejects_garbage_token(client: AsyncClient) -> None:
    resp = await client.get("/_test/me", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


async def test_me_returns_current_user(db_session: AsyncSession, client: AsyncClient) -> None:
    await _seed(db_session)
    token = (
        await client.post("/auth/login", json={"username": "user", "password": "userpw"})
    ).json()["access_token"]
    resp = await client.get("/_test/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"username": "user", "role": "user"}


async def test_admin_route_allows_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _seed(db_session)
    token = (
        await client.post("/auth/login", json={"username": "admin", "password": "adminpw"})
    ).json()["access_token"]
    resp = await client.get("/_test/admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


async def test_admin_route_forbids_user(db_session: AsyncSession, client: AsyncClient) -> None:
    await _seed(db_session)
    token = (
        await client.post("/auth/login", json={"username": "user", "password": "userpw"})
    ).json()["access_token"]
    resp = await client.get("/_test/admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
