"""Integration tests for temp-password recovery + forced change.

The recovery flow has three moving parts, all exercised here over the async
``db_session`` fixture:

1. an admin resets a user's password → a random temporary password (returned once)
   and ``must_change_password`` set;
2. enforcement → a flagged user is bounced (403) from every normal endpoint until
   they change it;
3. the change-password escape hatch → the flagged user sets a new password, which
   clears the flag and unblocks them.

Enforcement is probed against ``POST /repositories/{id}/query`` with a random repo
id: a flagged user is stopped at the auth dependency (403) *before* the handler
runs, whereas an unflagged user reaches the handler and gets 404 for the missing
repo — proving the bounce precedes endpoint logic.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import encrypt
from contextvault.core.security import verify_password
from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import LLMProviderName, ProviderSetting, Role, User
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


async def _user(
    db_session: AsyncSession,
    role: Role,
    username: str,
    *,
    password: str = "pw",
    must_change: bool = False,
) -> User:
    return await user_service.create_user(
        db_session,
        username=username,
        password=password,
        role=role,
        must_change_password=must_change,
    )


async def _token(client: AsyncClient, username: str, password: str = "pw") -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": password})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed_gemini_key(db_session: AsyncSession) -> None:
    """A verified Gemini key so the query endpoint's embedder dependency resolves.

    These tests probe /query only to prove the forced-change bounce; embeddings now
    require a global Gemini key, so seed one to let the unflagged request reach the
    404 handler instead of the 409 no-key gate.
    """
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.GEMINI,
            api_key_encrypted=encrypt("test-key"),
            verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.flush()


# --------------------------------------------------------------------------- #
# Admin temp-password reset
# --------------------------------------------------------------------------- #


async def test_admin_reset_returns_temp_password_and_sets_flag(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "victim")
    admin_token = await _token(client, "admin")

    resp = await client.post(f"/users/{target.id}/reset-password", headers=_auth(admin_token))
    assert resp.status_code == 200
    temp = resp.json()["temporary_password"]
    assert temp  # returned once, in plaintext

    await db_session.refresh(target)
    # The flag is set, and the stored hash is the temp password (not plaintext).
    assert target.must_change_password is True
    assert verify_password(temp, target.password_hash)

    # The user can log in with the temp password; login flags the forced change.
    login = await client.post("/auth/login", json={"username": "victim", "password": temp})
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


async def test_reset_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "regular")
    target = await _user(db_session, Role.USER, "other")
    token = await _token(client, "regular")
    resp = await client.post(f"/users/{target.id}/reset-password", headers=_auth(token))
    assert resp.status_code == 403


async def test_reset_requires_authentication(db_session: AsyncSession, client: AsyncClient) -> None:
    target = await _user(db_session, Role.USER, "lonely")
    resp = await client.post(f"/users/{target.id}/reset-password")
    assert resp.status_code == 401


async def test_reset_unknown_user_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    admin_token = await _token(client, "admin")
    resp = await client.post(f"/users/{uuid.uuid4()}/reset-password", headers=_auth(admin_token))
    assert resp.status_code == 404


async def test_temp_passwords_are_random(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "victim")
    admin_token = await _token(client, "admin")
    first = await client.post(f"/users/{target.id}/reset-password", headers=_auth(admin_token))
    second = await client.post(f"/users/{target.id}/reset-password", headers=_auth(admin_token))
    assert first.json()["temporary_password"] != second.json()["temporary_password"]


# --------------------------------------------------------------------------- #
# Enforcement — flagged users are bounced from normal endpoints
# --------------------------------------------------------------------------- #


async def test_flagged_user_is_bounced_from_protected_endpoint(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.USER, "flagged", must_change=True)
    token = await _token(client, "flagged")
    # Any authenticated endpoint is blocked at the auth gate, before its handler.
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/query", json={"question": "hi"}, headers=_auth(token)
    )
    assert resp.status_code == 403
    assert "password" in resp.json()["detail"].lower()


async def test_unflagged_user_is_not_bounced(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "ok")
    token = await _token(client, "ok")
    await _seed_gemini_key(db_session)
    # No forced change: the request reaches the handler, which 404s the missing repo.
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/query", json={"question": "hi"}, headers=_auth(token)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Change-password escape hatch — allowed while flagged, clears the flag
# --------------------------------------------------------------------------- #


async def test_flagged_user_can_change_password_and_is_unblocked(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db_session, Role.USER, "changer", password="temp-pass", must_change=True)
    token = await _token(client, "changer", password="temp-pass")

    resp = await client.post(
        "/auth/change-password",
        json={"current_password": "temp-pass", "new_password": "brand-new-password"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.must_change_password is False

    # New password works and no longer forces a change; old password is rejected.
    good = await client.post(
        "/auth/login", json={"username": "changer", "password": "brand-new-password"}
    )
    assert good.status_code == 200
    assert good.json()["must_change_password"] is False
    stale = await client.post("/auth/login", json={"username": "changer", "password": "temp-pass"})
    assert stale.status_code == 401

    # And the previously-bounced endpoint now reaches its handler (404 missing repo).
    await _seed_gemini_key(db_session)
    new_token = str(good.json()["access_token"])
    after = await client.post(
        f"/repositories/{uuid.uuid4()}/query", json={"question": "hi"}, headers=_auth(new_token)
    )
    assert after.status_code == 404


async def test_change_password_rejects_wrong_current(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db_session, Role.USER, "wrongcur", must_change=True)
    token = await _token(client, "wrongcur")
    resp = await client.post(
        "/auth/change-password",
        json={"current_password": "not-my-password", "new_password": "brand-new-password"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    await db_session.refresh(user)
    # A failed change leaves the account (and its forced-change flag) untouched.
    assert user.must_change_password is True


async def test_change_password_requires_authentication(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.post(
        "/auth/change-password",
        json={"current_password": "pw", "new_password": "brand-new-password"},
    )
    assert resp.status_code == 401


async def test_change_password_rejects_short_new_password(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.USER, "shorty")
    token = await _token(client, "shorty")
    resp = await client.post(
        "/auth/change-password",
        json={"current_password": "pw", "new_password": "short"},
        headers=_auth(token),
    )
    assert resp.status_code == 422
