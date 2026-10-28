"""Integration tests for the admin provider-settings API (global provider keys).

Keys are entered once per provider and verified against the live provider before being
stored. The provider's ``list_models`` call is the liveness check, so it is stubbed
here (a returning stub = a good key, a raising stub = a bad one). We assert verify-then-
store, that a bad key is rejected without being saved, masking, deletion, and access
control. Driven with httpx.AsyncClient over the async ``db_session`` fixture.
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import decrypt
from contextvault.db.session import get_session
from contextvault.llm.models import ModelListError
from contextvault.main import create_app
from contextvault.models import LLMProviderName, ProviderSetting, Role
from contextvault.services import users as user_service

_KEY = "sk-proj-abcdefghijklmnop4f2a"


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


def _stub_verify(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    """Stub the liveness check used when saving a key (ok=works, else raises)."""

    async def fake_list_models(
        provider: str, api_key: str, *, base_url: str | None = None
    ) -> list[str]:
        if not ok:
            raise ModelListError("Could not list models: invalid key")
        return ["gpt-4o", "gpt-4o-mini"]

    monkeypatch.setattr("contextvault.services.providers.list_models", fake_list_models)


async def test_list_providers_starts_all_unconfigured(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.get("/admin/providers", headers=_auth(token))
    assert resp.status_code == 200
    rows = resp.json()
    assert {r["provider"] for r in rows} == {
        "gemini",
        "openai",
        "openrouter",
        "anthropic",
        "custom",
    }
    assert all(r["configured"] is False and r["verified"] is False for r in rows)
    assert all(r["api_key_masked"] is None for r in rows)


async def test_list_providers_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(client, db_session, Role.USER)
    resp = await client.get("/admin/providers", headers=_auth(token))
    assert resp.status_code == 403


async def test_set_key_verifies_then_stores_masked(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.put("/admin/providers/openai", json={"api_key": _KEY}, headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["configured"] is True
    assert body["verified"] is True
    assert body["api_key_masked"] == "sk-…•••4f2a"
    assert _KEY not in resp.text

    # Stored as ciphertext, decryptable back to the entered key.
    setting = (
        await db_session.execute(
            ProviderSetting.__table__.select().where(
                ProviderSetting.provider == LLMProviderName.OPENAI
            )
        )
    ).first()
    assert setting is not None
    assert decrypt(setting.api_key_encrypted) == _KEY


async def test_set_key_rejects_a_bad_key_and_stores_nothing(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=False)
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.put(
        "/admin/providers/openai", json={"api_key": "bad"}, headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "Could not list models" in resp.json()["detail"]

    # Nothing persisted for a key that never verified.
    rows = (await client.get("/admin/providers", headers=_auth(token))).json()
    assert all(r["configured"] is False for r in rows)


async def test_get_reflects_a_saved_key(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)
    await client.put("/admin/providers/gemini", json={"api_key": _KEY}, headers=_auth(token))

    rows = {
        r["provider"]: r
        for r in (await client.get("/admin/providers", headers=_auth(token))).json()
    }
    assert rows["gemini"]["configured"] is True
    assert rows["gemini"]["verified"] is True
    assert rows["gemini"]["api_key_masked"] == "sk-…•••4f2a"
    assert rows["openai"]["configured"] is False


async def test_replacing_a_key_overwrites(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)
    await client.put("/admin/providers/openai", json={"api_key": _KEY}, headers=_auth(token))
    await client.put(
        "/admin/providers/openai", json={"api_key": "sk-second-key-9999"}, headers=_auth(token)
    )

    setting = (
        await db_session.execute(
            ProviderSetting.__table__.select().where(
                ProviderSetting.provider == LLMProviderName.OPENAI
            )
        )
    ).first()
    assert setting is not None
    assert decrypt(setting.api_key_encrypted) == "sk-second-key-9999"


async def test_delete_removes_the_key(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)
    await client.put("/admin/providers/openai", json={"api_key": _KEY}, headers=_auth(token))

    resp = await client.delete("/admin/providers/openai", headers=_auth(token))
    assert resp.status_code == 204

    rows = {
        r["provider"]: r
        for r in (await client.get("/admin/providers", headers=_auth(token))).json()
    }
    assert rows["openai"]["configured"] is False


async def test_delete_is_idempotent(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.delete("/admin/providers/openai", headers=_auth(token))
    assert resp.status_code == 204


async def test_set_key_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    token = await _token(client, db_session, Role.USER)
    resp = await client.put("/admin/providers/openai", json={"api_key": _KEY}, headers=_auth(token))
    assert resp.status_code == 403


async def test_unknown_provider_in_path_422(db_session: AsyncSession, client: AsyncClient) -> None:
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put(
        "/admin/providers/not-a-provider", json={"api_key": _KEY}, headers=_auth(token)
    )
    assert resp.status_code == 422


async def test_custom_requires_base_url(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put("/admin/providers/custom", json={"api_key": None}, headers=_auth(token))
    assert resp.status_code == 400
    assert "base url" in resp.json()["detail"].lower()


async def test_custom_saves_keyless_with_base_url(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put(
        "/admin/providers/custom",
        json={"base_url": "http://localhost:11434/v1"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "custom"
    assert body["verified"] is True
    assert body["base_url"] == "http://localhost:11434/v1"
    assert body["api_key_masked"] is None


async def test_cloud_provider_still_requires_key(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_verify(monkeypatch, ok=True)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put(
        "/admin/providers/openai", json={"base_url": None}, headers=_auth(token)
    )
    assert resp.status_code == 400
