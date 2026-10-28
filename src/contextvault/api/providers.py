"""Admin provider settings — one API key per LLM vendor (global provider keys).

API keys are entered once per provider here, not per repository. Saving a key verifies
it against the live provider before storing (a bad key is rejected with 400 and nothing
is saved); a stored key is returned only masked, never in full. Repositories then pick a
model from whichever providers are *verified* (``/repositories/{id}/llm-config``).

All routes are admin-only. The five providers always appear in the listing — those
without a key show ``configured: false`` — so the settings screen can render every row.
One of the five, ``custom`` (an OpenAI-compatible endpoint), is keyless-capable: it
stores an admin-supplied ``base_url`` and an *optional* key rather than a mandatory
one, so ``configured`` there reflects a saved ``base_url``, not a stored key.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.core.crypto import decrypt, mask_key
from contextvault.db.session import get_session
from contextvault.models import LLMProviderName, ProviderSetting, User
from contextvault.services import providers as provider_service

router = APIRouter(tags=["providers"])


class ProviderKeyRequest(BaseModel):
    """The config to store for a provider (verified before it is saved).

    ``api_key`` is optional so a keyless custom (OpenAI-compatible) endpoint can be
    saved with only a ``base_url``; cloud providers still require a key (enforced in
    the route, which knows the provider from the path)."""

    api_key: str | None = None
    base_url: str | None = None


class ProviderStatusResponse(BaseModel):
    """One provider's key state, with the key masked (never returned in full)."""

    provider: LLMProviderName
    configured: bool
    verified: bool
    api_key_masked: str | None
    base_url: str | None


def _status(provider: LLMProviderName, setting: ProviderSetting | None) -> ProviderStatusResponse:
    """Build a provider's status row, masking the stored key if present."""
    masked = (
        mask_key(decrypt(setting.api_key_encrypted))
        if setting and setting.api_key_encrypted is not None
        else None
    )
    return ProviderStatusResponse(
        provider=provider,
        configured=setting is not None,
        verified=bool(setting and setting.verified_at is not None),
        api_key_masked=masked,
        base_url=setting.base_url if setting else None,
    )


@router.get("/admin/providers")
async def list_providers(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ProviderStatusResponse]:
    """List every provider with its key status (admin-only). Always five rows."""
    by_provider = {s.provider: s for s in await provider_service.list_settings(session)}
    return [_status(p, by_provider.get(p)) for p in LLMProviderName]


@router.put("/admin/providers/{provider}")
async def set_provider(
    provider: LLMProviderName,
    payload: ProviderKeyRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ProviderStatusResponse:
    """Store (and first verify) a provider's API key (admin-only).

    The key is checked against the live provider; a key that does not work is rejected
    with 400 and nothing is stored. On success it is saved encrypted and marked verified.
    A custom (OpenAI-compatible) endpoint requires a ``base_url`` but may be keyless;
    cloud providers still require a key.
    """
    key = (payload.api_key or "").strip() or None
    base_url = (payload.base_url or "").strip() or None
    if provider == LLMProviderName.CUSTOM:
        if not base_url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A base URL is required for a custom OpenAI-compatible endpoint.",
            )
    elif not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="API key is required.")
    try:
        setting = await provider_service.set_provider_key(
            session, provider, key, now=datetime.now(UTC), base_url=base_url
        )
    except provider_service.ProviderKeyInvalid as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _status(provider, setting)


@router.delete("/admin/providers/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(
    provider: LLMProviderName,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a provider's stored key (admin-only). Idempotent — 204 even if none."""
    await provider_service.delete_provider_key(session, provider)
