"""Global provider-key settings — store, verify, and hand out one key per vendor.

The single place that touches ``ProviderSetting``. Keys are entered once per provider
(design: global provider settings), verified against the live provider before being
stored, kept as Fernet ciphertext, and decrypted only here at call time. The query
loop and image OCR both resolve a repository's key through :func:`get_provider_key`,
so neither depends on where the key is stored.

Saving a key is verify-then-store: :func:`set_provider_key` asks the provider for its
model list with the entered key (reusing :func:`contextvault.llm.models.list_models`)
and refuses to store a key that does not work, raising :class:`ProviderKeyInvalid`.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.core.crypto import decrypt, encrypt
from contextvault.llm.models import ModelListError, list_models
from contextvault.models import LLMProviderName, ProviderSetting, Repository


class ProviderKeyInvalid(Exception):
    """The supplied API key failed its live check against the provider."""


# OpenAI-compatible servers require *some* Authorization header even when they
# ignore it; a keyless local endpoint gets this harmless placeholder at call time.
# It is never persisted.
NOAUTH_PLACEHOLDER = "sk-noauth"


def _static_base_url(provider: LLMProviderName) -> str | None:
    """The fixed OpenAI-compatible base URL a provider needs from settings (OpenRouter
    only). Custom endpoints are per-row and resolved via ``get_provider_base_url``."""
    return get_settings().openrouter_base_url if provider == LLMProviderName.OPENROUTER else None


async def get_setting(session: AsyncSession, provider: LLMProviderName) -> ProviderSetting | None:
    """The stored setting row for ``provider``, or ``None`` if no key is set."""
    result = await session.execute(
        sa.select(ProviderSetting).where(ProviderSetting.provider == provider)
    )
    return result.scalar_one_or_none()


async def list_settings(session: AsyncSession) -> list[ProviderSetting]:
    """Every stored provider setting, ordered by provider name."""
    result = await session.execute(sa.select(ProviderSetting).order_by(ProviderSetting.provider))
    return list(result.scalars().all())


async def verified_provider_names(session: AsyncSession) -> set[LLMProviderName]:
    """The providers that currently have a stored, verified key.

    A repository may only pick a model from one of these — its key is what makes
    the repository answerable."""
    result = await session.execute(
        sa.select(ProviderSetting.provider).where(ProviderSetting.verified_at.is_not(None))
    )
    return set(result.scalars().all())


async def repo_is_answerable(session: AsyncSession, repo: Repository) -> bool:
    """True when ``repo`` can actually answer: a model is picked *and* its provider
    has a verified key. This is the predicate the query and image-upload paths gate
    on (the model choice alone is not enough — the shared key must exist)."""
    if repo.llm_provider is None or not repo.llm_selected:
        return False
    return repo.llm_provider in await verified_provider_names(session)


async def get_provider_base_url(session: AsyncSession, provider: LLMProviderName) -> str | None:
    """The OpenAI-compatible base URL for ``provider`` at call time.

    Custom endpoints store their address on the row; OpenRouter uses the settings
    default; every other provider talks to its SDK's own endpoint (``None``)."""
    if provider == LLMProviderName.CUSTOM:
        setting = await get_setting(session, provider)
        return setting.base_url if setting else None
    return _static_base_url(provider)


async def get_provider_key(session: AsyncSession, provider: LLMProviderName) -> str | None:
    """The decrypted key for ``provider``, or ``None`` when none is stored (a keyless
    custom endpoint stores no key). Decryption happens only here."""
    setting = await get_setting(session, provider)
    if setting is None or setting.api_key_encrypted is None:
        return None
    return decrypt(setting.api_key_encrypted)


async def get_call_credentials(
    session: AsyncSession, provider: LLMProviderName
) -> tuple[str, str | None]:
    """The ``(api_key, base_url)`` to construct a client for ``provider``.

    A keyless custom endpoint yields the placeholder key so the client still sends
    an Authorization header. Cloud providers are gated on a real key upstream, so
    the placeholder is never reached for them."""
    key = await get_provider_key(session, provider)
    base_url = await get_provider_base_url(session, provider)
    return key or NOAUTH_PLACEHOLDER, base_url


async def verify_key(
    provider: LLMProviderName, api_key: str | None, *, base_url: str | None = None
) -> None:
    """Check the endpoint answers, raising :class:`ProviderKeyInvalid` if not.

    ``base_url`` is the endpoint being saved (custom) — it isn't in the DB yet, so
    it is passed in. A keyless custom endpoint is verified with the placeholder key."""
    try:
        await list_models(
            provider.value,
            api_key or NOAUTH_PLACEHOLDER,
            base_url=base_url or _static_base_url(provider),
        )
    except ModelListError as exc:
        raise ProviderKeyInvalid(str(exc)) from exc


async def set_provider_key(
    session: AsyncSession,
    provider: LLMProviderName,
    api_key: str | None,
    *,
    now: datetime,
    base_url: str | None = None,
) -> ProviderSetting:
    """Verify then store ``provider``'s config (upsert), stamping ``verified_at``.

    ``api_key`` may be ``None`` for a keyless custom endpoint; ``base_url`` is stored
    for custom (``None`` for cloud providers). Stores nothing if verification fails."""
    await verify_key(provider, api_key, base_url=base_url)

    setting = await get_setting(session, provider)
    if setting is None:
        setting = ProviderSetting(provider=provider)
        session.add(setting)
    setting.api_key_encrypted = encrypt(api_key) if api_key else None
    setting.base_url = base_url
    setting.verified_at = now
    await session.commit()
    await session.refresh(setting)
    return setting


async def delete_provider_key(session: AsyncSession, provider: LLMProviderName) -> bool:
    """Remove ``provider``'s stored key. Returns ``False`` if there was none."""
    setting = await get_setting(session, provider)
    if setting is None:
        return False
    await session.delete(setting)
    await session.commit()
    return True
