"""Integration tests for the onboarding invitation endpoints.

Covers the whole invite lifecycle over httpx.AsyncClient on the async
``db_session`` fixture: an admin issues a single-use, expiring invite (and only an
admin can); the invited user redeems it by choosing their own password, which
creates a working account; and reused, expired, or unknown tokens are all
rejected with one uniform error.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Invitation, Role, User
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _admin_token(db_session: AsyncSession, client: AsyncClient, name: str = "admin") -> str:
    await _user(db_session, Role.ADMIN, name)
    return await _token(client, name)


async def test_create_invitation_requires_authentication(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.post("/invitations", json={"username": "newbie"})
    assert resp.status_code == 401


async def test_create_invitation_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.USER, "regular")
    token = await _token(client, "regular")
    resp = await client.post("/invitations", json={"username": "newbie"}, headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_creates_single_use_expiring_invite(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_token = await _admin_token(db_session, client)
    resp = await client.post(
        "/invitations",
        json={"username": "invitee", "role": "user"},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201
    body = resp.json()
    # The raw token is returned once for the invite link.
    assert body["token"]
    assert body["username"] == "invitee"
    assert body["role"] == "user"
    # A future expiry makes the invite time-boxed.
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)

    # Only the hash is persisted — never the raw token.
    stored = (await db_session.execute(sa.select(Invitation))).scalar_one()
    assert stored.token_hash != body["token"]
    assert stored.accepted_at is None


async def test_create_invitation_rejects_existing_username(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_token = await _admin_token(db_session, client)
    await _user(db_session, Role.USER, "taken")
    resp = await client.post("/invitations", json={"username": "taken"}, headers=_auth(admin_token))
    assert resp.status_code == 409


async def test_accept_invite_creates_working_account(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_token = await _admin_token(db_session, client)
    created = await client.post(
        "/invitations", json={"username": "alice"}, headers=_auth(admin_token)
    )
    token = created.json()["token"]

    resp = await client.post(
        "/invitations/accept", json={"token": token, "password": "her-own-password"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["username"] == "alice"
    assert body["role"] == "user"
    assert uuid.UUID(body["id"])

    # The account is real and usable with the password the user chose.
    login = await client.post(
        "/auth/login", json={"username": "alice", "password": "her-own-password"}
    )
    assert login.status_code == 200

    # The invite is now spent (single-use): accepted_at is stamped.
    stored = (await db_session.execute(sa.select(Invitation))).scalar_one()
    assert stored.accepted_at is not None


async def test_accepted_invite_cannot_be_reused(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_token = await _admin_token(db_session, client)
    created = await client.post(
        "/invitations", json={"username": "bob"}, headers=_auth(admin_token)
    )
    token = created.json()["token"]

    first = await client.post("/invitations/accept", json={"token": token, "password": "password1"})
    assert first.status_code == 201

    # A second redemption of the same token is rejected — single-use.
    second = await client.post(
        "/invitations/accept", json={"token": token, "password": "password2"}
    )
    assert second.status_code == 400


async def test_expired_invite_is_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    admin_token = await _admin_token(db_session, client)
    created = await client.post(
        "/invitations", json={"username": "carol"}, headers=_auth(admin_token)
    )
    token = created.json()["token"]

    # Force the invite into the past, then try to redeem it.
    invitation = (await db_session.execute(sa.select(Invitation))).scalar_one()
    invitation.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.flush()

    resp = await client.post("/invitations/accept", json={"token": token, "password": "password1"})
    assert resp.status_code == 400
    # An expired invite never creates an account.
    assert await user_service.get_user_by_username(db_session, "carol") is None


async def test_unknown_token_is_rejected(db_session: AsyncSession, client: AsyncClient) -> None:
    resp = await client.post(
        "/invitations/accept", json={"token": "not-a-real-token", "password": "password1"}
    )
    assert resp.status_code == 400


async def test_accept_rejects_too_short_password(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    admin_token = await _admin_token(db_session, client)
    created = await client.post(
        "/invitations", json={"username": "dave"}, headers=_auth(admin_token)
    )
    token = created.json()["token"]

    resp = await client.post("/invitations/accept", json={"token": token, "password": "short"})
    assert resp.status_code == 422
    # The invite is untouched — a rejected password does not spend it.
    stored = (await db_session.execute(sa.select(Invitation))).scalar_one()
    assert stored.accepted_at is None
