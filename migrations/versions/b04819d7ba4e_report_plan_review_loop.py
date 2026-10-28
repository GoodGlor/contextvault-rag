"""report plan review loop

Revision ID: b04819d7ba4e
Revises: 431cbcc7304a
Create Date: 2026-08-01 19:05:23.911042
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b04819d7ba4e'
down_revision: str | None = '431cbcc7304a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE report_status ADD VALUE IF NOT EXISTS 'planning'")
    op.execute("ALTER TYPE report_status ADD VALUE IF NOT EXISTS 'awaiting_approval'")
    op.add_column("generated_reports", sa.Column("plan", postgresql.JSONB(), nullable=True))
    op.add_column(
        "generated_reports",
        sa.Column("plan_rounds", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    # Postgres cannot drop an enum label; the columns go, the labels stay. Harmless —
    # no row can hold one after the columns are gone.
    op.drop_column("generated_reports", "plan_rounds")
    op.drop_column("generated_reports", "plan")
