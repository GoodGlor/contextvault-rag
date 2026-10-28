"""image & web source kinds + source_url

Revision ID: a1b2c3d4e5f6
Revises: 550f1a28b886
Create Date: 2026-07-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "550f1a28b886"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block, so commit
    # first and use IF NOT EXISTS so the migration is safely re-runnable.
    op.execute("COMMIT")
    op.execute("ALTER TYPE source_kind ADD VALUE IF NOT EXISTS 'image'")
    op.execute("ALTER TYPE source_kind ADD VALUE IF NOT EXISTS 'web'")
    op.add_column("sources", sa.Column("source_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    # Postgres cannot drop a single enum value; only the column is reversible.
    op.drop_column("sources", "source_url")
