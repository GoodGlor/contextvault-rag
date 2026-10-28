"""Tests for the provider-settings service: custom (OpenAI-compatible) support.

Covers the data-layer additions (a nullable base_url and a now-optional key) plus
the service seams that resolve a base URL and call credentials, including the
keyless-local-server path. DB-backed tests use the rolled-back ``db_session``.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import LLMProviderName, ProviderSetting
from contextvault.services import providers as provider_service


def test_custom_is_a_provider_value() -> None:
    assert LLMProviderName.CUSTOM.value == "custom"


async def test_custom_row_persists_with_base_url_and_no_key(db_session: AsyncSession) -> None:
    row = ProviderSetting(
        provider=LLMProviderName.CUSTOM,
        api_key_encrypted=None,
        base_url="http://localhost:11434/v1",
        verified_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.api_key_encrypted is None
    assert row.base_url == "http://localhost:11434/v1"


async def test_get_provider_base_url_reads_custom_row(db_session: AsyncSession) -> None:
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.CUSTOM,
            api_key_encrypted=None,
            base_url="http://gpu-box:8000/v1",
            verified_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    assert (
        await provider_service.get_provider_base_url(db_session, LLMProviderName.CUSTOM)
        == "http://gpu-box:8000/v1"
    )


async def test_call_credentials_uses_placeholder_when_keyless(db_session: AsyncSession) -> None:
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.CUSTOM,
            api_key_encrypted=None,
            base_url="http://gpu-box:8000/v1",
            verified_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    key, base_url = await provider_service.get_call_credentials(db_session, LLMProviderName.CUSTOM)
    assert key == provider_service.NOAUTH_PLACEHOLDER
    assert base_url == "http://gpu-box:8000/v1"


async def test_set_custom_stores_base_url_and_optional_key(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def ok_list_models(
        provider: str, api_key: str, *, base_url: str | None = None
    ) -> list[str]:
        return ["llama3.1:8b"]

    monkeypatch.setattr("contextvault.services.providers.list_models", ok_list_models)
    setting = await provider_service.set_provider_key(
        db_session,
        LLMProviderName.CUSTOM,
        None,  # keyless
        now=datetime.now(UTC),
        base_url="http://localhost:11434/v1",
    )
    assert setting.base_url == "http://localhost:11434/v1"
    assert setting.api_key_encrypted is None
    assert setting.verified_at is not None
