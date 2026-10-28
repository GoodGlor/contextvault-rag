"""Integration tests for the admin user-list endpoint.

The admin user-management UI needs to enumerate accounts (to reset/delete them
and to pick grant recipients); `GET /users` is that admin-only listing. Mirrors
the real-auth httpx harness used across the API tests.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Role
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


async def test_list_requires_authentication(client: AsyncClient) -> None:
    resp = await client.get("/users")
    assert resp.status_code == 401


async def test_list_forbidden_for_non_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.USER)
    resp = await client.get("/users", headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_lists_all_users(db_session: AsyncSession, client: AsyncClient) -> None:
    # A regular user (owes a password change) plus the admin created for the token.
    await user_service.create_user(
        db_session, username="member", password="pw", role=Role.USER, must_change_password=True
    )
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.get("/users", headers=_auth(token))
    assert resp.status_code == 200
    by_name = {u["username"]: u for u in resp.json()}
    assert {"member", "adminuser"} <= set(by_name)
    member = by_name["member"]
    assert member["role"] == "user"
    assert member["must_change_password"] is True
    assert "id" in member and "created_at" in member
    assert by_name["adminuser"]["role"] == "admin"
    # Never leaks password hashes.
    assert "password_hash" not in member
