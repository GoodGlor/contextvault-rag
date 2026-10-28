"""Integration tests for admin repository management.

Admins create repositories and list *all* of them (each with its LLM-config
state) so the admin UI can drive create → configure. Unlike ``GET /repositories``
(the user's granted picker), ``GET /admin/repositories`` is admin-only and returns
every repository. Mirrors test_repositories_api's real-auth httpx harness.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import LLMProviderName, ProviderSetting, Repository, Role, User
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


# --- create -----------------------------------------------------------------


async def test_create_requires_authentication(client: AsyncClient) -> None:
    resp = await client.post("/repositories", json={"name": "Handbook"})
    assert resp.status_code == 401


async def test_create_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(client, db_session, Role.USER)
    resp = await client.post("/repositories", json={"name": "Handbook"}, headers=_auth(token))
    assert resp.status_code == 403


async def test_admin_creates_repository(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.post(
        "/repositories",
        json={"name": "Handbook", "description": "the company handbook"},
        headers=_auth(token),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Handbook"
    assert body["description"] == "the company handbook"
    assert "id" in body
    # A freshly created repo has no LLM config yet.
    assert body["configured"] is False
    # Persisted.
    repo = await db_session.get(Repository, uuid.UUID(body["id"]))
    assert repo is not None and repo.name == "Handbook"


async def test_create_grants_the_creating_admin_access(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Without an auto-grant, the admin who just created the repo has
    no Grant on it and can't use it until they grant it to themselves. Creating it
    should immediately give the creator active access."""
    token = await _token(client, db_session, Role.ADMIN)
    admin = await user_service.get_user_by_username(db_session, "adminuser")
    assert isinstance(admin, User)

    resp = await client.post(
        "/repositories",
        json={"name": "Handbook"},
        headers=_auth(token),
    )
    assert resp.status_code == 201
    repo_id = uuid.UUID(resp.json()["id"])

    assert await grant_service.has_active_grant(db_session, admin.id, repo_id) is True

    picker = await client.get("/repositories", headers=_auth(token))
    assert picker.status_code == 200
    assert any(r["id"] == str(repo_id) for r in picker.json())


async def test_create_defaults_description_to_null(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.post("/repositories", json={"name": "Runbook"}, headers=_auth(token))
    assert resp.status_code == 201
    assert resp.json()["description"] is None


async def test_create_rejects_blank_name(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.post("/repositories", json={"name": ""}, headers=_auth(token))
    assert resp.status_code == 422


# --- admin list-all ---------------------------------------------------------


async def test_list_all_requires_authentication(client: AsyncClient) -> None:
    resp = await client.get("/admin/repositories")
    assert resp.status_code == 401


async def test_list_all_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(client, db_session, Role.USER)
    resp = await client.get("/admin/repositories", headers=_auth(token))
    assert resp.status_code == 403


async def test_list_all_returns_every_repo_with_config_state(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # "Ready" is answerable: a model picked whose provider has a verified key.
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.OPENAI,
            api_key_encrypted=encrypt("sk-proj-secret"),
            verified_at=datetime.now(UTC),
        )
    )
    unconfigured = Repository(name="Empty")
    configured = Repository(
        name="Ready",
        description="ops",
        llm_provider=LLMProviderName.OPENAI,
        llm_model="gpt-4o",
    )
    # "Picked but no key": a model chosen for a provider with NO verified key is NOT
    # answerable — proves ``configured`` spans both tables.
    picked_no_key = Repository(
        name="Picked", llm_provider=LLMProviderName.ANTHROPIC, llm_model="claude-opus-4-8"
    )
    db_session.add_all([unconfigured, configured, picked_no_key])
    await db_session.flush()
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.get("/admin/repositories", headers=_auth(token))
    assert resp.status_code == 200
    by_name = {r["name"]: r for r in resp.json()}
    assert set(by_name) == {"Empty", "Ready", "Picked"}
    assert by_name["Empty"]["configured"] is False
    assert by_name["Ready"]["configured"] is True
    assert by_name["Picked"]["configured"] is False
    assert by_name["Ready"]["description"] == "ops"
    # The list never leaks key material.
    assert "sk-proj-secret" not in resp.text
