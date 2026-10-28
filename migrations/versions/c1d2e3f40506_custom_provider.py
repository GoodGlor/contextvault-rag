"""custom openai-compatible provider: enum value, base_url, nullable key

Adds the ``custom`` value to the ``llm_provider`` enum, a nullable ``base_url``
column on ``provider_settings`` (the endpoint address for a self-hosted server),
and relaxes ``api_key_encrypted`` to nullable so a keyless local server can be
stored (base URL only).

Revision ID: c1d2e3f40506
Revises: b8c2d5e7f901
Create Date: 2026-07-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f40506"
down_revision: str | None = "b8c2d5e7f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so commit
    # first and use IF NOT EXISTS so the migration is safely re-runnable — the same
    # pattern as migrations/versions/a1b2c3d4e5f6_image_web_sources.py.
    op.execute("COMMIT")
    op.execute("ALTER TYPE llm_provider ADD VALUE IF NOT EXISTS 'custom'")
    op.add_column("provider_settings", sa.Column("base_url", sa.Text(), nullable=True))
    op.alter_column(
        "provider_settings", "api_key_encrypted", existing_type=sa.Text(), nullable=True
    )


def downgrade() -> None:
    # Postgres cannot drop a single enum value, so 'custom' is left in the type
    # (harmless, unused). Re-tightening the key column will fail if a keyless custom
    # row exists; that is acceptable for a downgrade and documented here.
    op.alter_column(
        "provider_settings", "api_key_encrypted", existing_type=sa.Text(), nullable=False
    )
    op.drop_column("provider_settings", "base_url")
