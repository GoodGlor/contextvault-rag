"""global provider settings: per-provider keys, drop per-repo key

Move LLM API keys off individual repositories and into one row per provider
(``provider_settings``). Repositories keep ``llm_provider`` / ``llm_model`` (which
model they answer with) but no longer carry their own key — the key is shared from
the provider settings. Existing per-repo keys are not migrated: an admin re-enters
each provider's key once in the new Providers settings.

Revision ID: d4f1a2b7c9e0
Revises: a1b2c3d4e5f6
Create Date: 2026-07-23 09:40:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd4f1a2b7c9e0'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Reuse the existing ``llm_provider`` enum type (created with the repository
    # LLM-config columns); ``create_type=False`` references it by name without
    # re-emitting CREATE TYPE (which would fail — the type already exists).
    provider_enum = postgresql.ENUM(
        "gemini", "openai", "openrouter", "anthropic", name="llm_provider", create_type=False
    )
    op.create_table(
        "provider_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", provider_enum, nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", name="uq_provider_settings_provider"),
    )
    # Keys now live per-provider; drop the per-repository key column.
    op.drop_column("repositories", "api_key_encrypted")


def downgrade() -> None:
    op.add_column("repositories", sa.Column("api_key_encrypted", sa.Text(), nullable=True))
    op.drop_table("provider_settings")
