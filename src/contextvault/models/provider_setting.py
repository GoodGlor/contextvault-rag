"""Global per-provider LLM credentials (one API key per vendor).

API keys live here — once per provider — rather than on each repository: an admin
fills in a provider's key in the Providers settings, ContextVault verifies it works,
and every repository that picks a model from that provider reuses this one key. The
key is Fernet ciphertext at rest (``core/crypto.py``), decrypted only in memory at
call time, and never returned in full. A custom (OpenAI-compatible) endpoint may be
keyless — a stored base URL with no key — since some self-hosted servers require no
credential.

``verified_at`` records when the stored key last passed a live check (a successful
provider ``list_models`` call). It is set on every successful save and is the signal
the UI shows as *Verified* and that repo configuration gates on.
"""

from datetime import datetime

from sqlalchemy import DateTime, Enum, Text
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.enums import LLMProviderName
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ProviderSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One vendor's stored, verified API key — shared by every repo using it."""

    __tablename__ = "provider_settings"

    # One row per provider: the provider name is the natural unique key.
    provider: Mapped[LLMProviderName] = mapped_column(
        Enum(LLMProviderName, name="llm_provider", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        unique=True,
    )
    api_key_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Custom (OpenAI-compatible) endpoints store their address here; it is not a
    # secret (never encrypted) and, unlike the key, is returned in status responses.
    # NULL for the cloud providers, which use fixed/hardcoded endpoints.
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stamped whenever the stored key last passed a live provider check; a key is
    # only ever stored after it verifies, so this is always set for a stored row.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
