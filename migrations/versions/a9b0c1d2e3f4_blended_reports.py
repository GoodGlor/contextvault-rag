"""blended reports: nullable connection_id + sections audit column

Revision ID: a9b0c1d2e3f4
Revises: c1d2e3f40506
Create Date: 2026-07-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "c1d2e3f40506"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A document-only report has no DB connection.
    op.alter_column(
        "generated_reports", "connection_id", existing_type=sa.Uuid(), nullable=True
    )
    op.add_column(
        "generated_reports",
        sa.Column("sections", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generated_reports", "sections")
    # Restoring NOT NULL requires removing document-only rows first — destructive
    # on downgrade, the same accepted posture as enum-value migrations.
    op.execute("DELETE FROM generated_reports WHERE connection_id IS NULL")
    op.alter_column(
        "generated_reports", "connection_id", existing_type=sa.Uuid(), nullable=False
    )
