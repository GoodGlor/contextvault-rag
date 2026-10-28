"""``get_embedder`` resolves the global Gemini key or hard-fails 409."""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_embedder
from contextvault.core.crypto import encrypt
from contextvault.embeddings.gemini import GeminiEmbeddingProvider
from contextvault.models import LLMProviderName, ProviderSetting


async def test_get_embedder_raises_409_without_gemini_key(db_session: AsyncSession) -> None:
    with pytest.raises(HTTPException) as exc:
        await get_embedder(session=db_session)
    assert exc.value.status_code == 409
    assert "Gemini" in exc.value.detail


async def test_get_embedder_builds_provider_with_key(db_session: AsyncSession) -> None:
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.GEMINI,
            api_key_encrypted=encrypt("secret-key"),
            verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await db_session.flush()

    provider = await get_embedder(session=db_session)
    assert isinstance(provider, GeminiEmbeddingProvider)
    assert provider.dimension == 1024
