"""report sections data

Revision ID: 6127fc765f9a
Revises: b04819d7ba4e
Create Date: 2026-08-03 19:16:59.407080
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '6127fc765f9a'
down_revision: str | None = 'b04819d7ba4e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_reports",
        sa.Column("sections_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("generated_reports", "sections_data")
