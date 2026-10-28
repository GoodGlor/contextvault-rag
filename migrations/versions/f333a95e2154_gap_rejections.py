"""gap_rejections

Revision ID: f333a95e2154
Revises: f170138d3652
Create Date: 2026-07-23 15:08:36.288016
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'f333a95e2154'
down_revision: str | None = 'f170138d3652'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gap_rejections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("normalized_question", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rejected_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rejected_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", "normalized_question", name="uq_gap_rejections_repository_id_normalized_question"),
    )
    op.create_index("ix_gap_rejections_repository_id", "gap_rejections", ["repository_id"])
    op.create_index("ix_gap_rejections_rejected_by", "gap_rejections", ["rejected_by"])


def downgrade() -> None:
    op.drop_table("gap_rejections")
