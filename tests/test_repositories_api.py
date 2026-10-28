"""Integration tests for the admin per-repo LLM-*model* API (revised).

A repository no longer stores its own key — it picks a provider + model, and the key
is shared from the global provider settings (see test_providers_api). So configuring a
repo means choosing a model whose provider already has a verified key; there is no key
in these requests or responses. Driven with httpx.AsyncClient over the async
``db_session`` fixture, mirroring test_sources_api's real-auth pattern.
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
from contextvault.models import LLMProviderName, ProviderSetting, Repository, Role
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


async def _repo(db_session: AsyncSession) -> Repository:
    repo = Repository(name="Vault")
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _verify_provider(
    db_session: AsyncSession, provider: LLMProviderName = LLMProviderName.OPENAI
) -> None:
    """Seed a stored, verified key for ``provider`` (as the Providers settings would)."""
    db_session.add(
        ProviderSetting(
            provider=provider,
            api_key_encrypted=encrypt("sk-stored"),
            verified_at=datetime.now(UTC),
        )
    )
    await db_session.flush()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _config(provider: str = "openai", model: str = "gpt-4o") -> dict[str, str]:
    return {"provider": provider, "model": model}


async def test_set_config_requires_authentication(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    resp = await client.put(f"/repositories/{repo.id}/llm-config", json=_config())
    assert resp.status_code == 401


async def test_set_config_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.USER)
    resp = await client.put(
        f"/repositories/{repo.id}/llm-config", json=_config(), headers=_auth(token)
    )
    assert resp.status_code == 403


async def test_admin_picks_model_from_a_verified_provider(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.put(
        f"/repositories/{repo.id}/llm-config", json=_config(), headers=_auth(token)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o"
    assert body["configured"] is True
    # No key is ever part of the config response — keys live in Providers settings.
    assert "api_key_masked" not in body

    await db_session.refresh(repo)
    assert repo.llm_provider == LLMProviderName.OPENAI
    assert repo.llm_model == "gpt-4o"


async def test_set_config_rejects_provider_without_a_verified_key(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # The provider has no stored key, so the repo can't use it: 400, nothing saved.
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put(
        f"/repositories/{repo.id}/llm-config", json=_config(), headers=_auth(token)
    )
    assert resp.status_code == 400
    await db_session.refresh(repo)
    assert repo.llm_provider is None
    assert repo.llm_model is None


async def test_update_model_overwrites_previous(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    await client.put(f"/repositories/{repo.id}/llm-config", json=_config(), headers=_auth(token))
    resp = await client.put(
        f"/repositories/{repo.id}/llm-config",
        json=_config(model="gpt-4o-mini"),
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "gpt-4o-mini"
    await db_session.refresh(repo)
    assert repo.llm_model == "gpt-4o-mini"


async def test_get_config_returns_model_and_answerability(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    await client.put(f"/repositories/{repo.id}/llm-config", json=_config(), headers=_auth(token))

    resp = await client.get(f"/repositories/{repo.id}/llm-config", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o"


async def test_get_config_unconfigured_repo(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.get(f"/repositories/{repo.id}/llm-config", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["provider"] is None
    assert body["model"] is None


async def test_get_config_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.USER)
    resp = await client.get(f"/repositories/{repo.id}/llm-config", headers=_auth(token))
    assert resp.status_code == 403


async def test_set_config_unknown_repository_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put(
        f"/repositories/{uuid.uuid4()}/llm-config", json=_config(), headers=_auth(token)
    )
    assert resp.status_code == 404


async def test_set_config_rejects_unknown_provider(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.put(
        f"/repositories/{repo.id}/llm-config",
        json=_config(provider="not-a-provider"),
        headers=_auth(token),
    )
    assert resp.status_code == 422


# --- list-models endpoint (uses the provider's global key) ------------------


async def test_list_models_uses_the_global_provider_key(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    seen: dict[str, object] = {}

    async def fake_list_models(
        provider: str, api_key: str, *, base_url: str | None = None
    ) -> list[str]:
        seen["provider"], seen["api_key"] = provider, api_key
        return ["gpt-4o", "o3-mini"]

    monkeypatch.setattr("contextvault.api.repositories.list_models", fake_list_models)
    resp = await client.post(
        f"/repositories/{repo.id}/llm-models",
        json={"provider": "openai"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["models"] == ["gpt-4o", "o3-mini"]
    # The stored (decrypted) global key is used — the client never sends one.
    assert seen == {"provider": "openai", "api_key": "sk-stored"}


async def test_list_models_no_key_for_provider_400(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)  # no provider keys stored
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.post(
        f"/repositories/{repo.id}/llm-models",
        json={"provider": "openai"},
        headers=_auth(token),
    )
    assert resp.status_code == 400


async def test_list_models_provider_error_is_400(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextvault.llm.models import ModelListError

    repo = await _repo(db_session)
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    async def boom(provider: str, api_key: str, *, base_url: str | None = None) -> list[str]:
        raise ModelListError("Could not list models: invalid key")

    monkeypatch.setattr("contextvault.api.repositories.list_models", boom)
    resp = await client.post(
        f"/repositories/{repo.id}/llm-models",
        json={"provider": "openai"},
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert "Could not list models" in resp.json()["detail"]


async def test_list_models_forbidden_for_non_admin(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.USER)
    resp = await client.post(
        f"/repositories/{repo.id}/llm-models",
        json={"provider": "openai"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_list_models_unknown_repo_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _verify_provider(db_session)
    token = await _token(client, db_session, Role.ADMIN)
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/llm-models",
        json={"provider": "openai"},
        headers=_auth(token),
    )
    assert resp.status_code == 404


# --- report-template endpoint (repository default template) ------


async def test_admin_sets_the_repository_report_template(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.put(
        f"/repositories/{repo.id}/report-template",
        json={"template": "prezydia"},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    assert resp.json() == {"template": "prezydia"}


async def test_the_saved_template_reads_back(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    await client.put(
        f"/repositories/{repo.id}/report-template",
        json={"template": "minimum"},
        headers=_auth(token),
    )
    resp = await client.get(f"/repositories/{repo.id}/report-template", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json() == {"template": "minimum"}


async def test_setting_the_template_to_null_clears_it(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Before this fix, ``template`` was ``str`` with ``min_length=1``, so a
    default once set could never be unset through the API — a one-way door that
    needed direct SQL to undo."""
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    await client.put(
        f"/repositories/{repo.id}/report-template",
        json={"template": "minimum"},
        headers=_auth(token),
    )
    resp = await client.put(
        f"/repositories/{repo.id}/report-template",
        json={"template": None},
        headers=_auth(token),
    )

    assert resp.status_code == 200
    assert resp.json() == {"template": None}

    get_resp = await client.get(f"/repositories/{repo.id}/report-template", headers=_auth(token))
    assert get_resp.json() == {"template": None}


async def test_a_repository_with_no_choice_reads_back_null(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.get(f"/repositories/{repo.id}/report-template", headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json() == {"template": None}


async def test_an_unknown_template_is_rejected_at_write_time(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Writes reject where reads tolerate — the caller is here to be told."""
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.ADMIN)

    resp = await client.put(
        f"/repositories/{repo.id}/report-template",
        json={"template": "no-such-template"},
        headers=_auth(token),
    )

    assert resp.status_code == 400
    assert "kvarta" in resp.json()["detail"]


async def test_a_non_admin_cannot_set_the_report_template(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    token = await _token(client, db_session, Role.USER)

    resp = await client.put(
        f"/repositories/{repo.id}/report-template",
        json={"template": "minimum"},
        headers=_auth(token),
    )

    assert resp.status_code == 403
