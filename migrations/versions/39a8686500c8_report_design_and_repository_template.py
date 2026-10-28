"""report design and repository template

Revision ID: 39a8686500c8
Revises: 6127fc765f9a
Create Date: 2026-08-04 15:54:16.437525
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '39a8686500c8'
down_revision: str | None = '6127fc765f9a'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generated_reports",
        sa.Column("design", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "repositories",
        sa.Column("report_template", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("repositories", "report_template")
    op.drop_column("generated_reports", "design")
