"""report pptx artifact

Adds the second download format's artifact columns. Autogenerate also proposed
dropping and recreating the ``conversations`` and ``gap_rejections`` unique
constraints — that is naming-convention drift, not a schema change, and
rewriting live constraints to rename them is not worth the lock. Removed.

Revision ID: 431cbcc7304a
Revises: e5a7b3c91d24
Create Date: 2026-08-01 17:44:03.028976
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "431cbcc7304a"
down_revision: str | None = "e5a7b3c91d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generated_reports", sa.Column("pptx_data", sa.LargeBinary(), nullable=True))
    op.add_column(
        "generated_reports", sa.Column("pptx_filename", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("generated_reports", "pptx_filename")
    op.drop_column("generated_reports", "pptx_data")
