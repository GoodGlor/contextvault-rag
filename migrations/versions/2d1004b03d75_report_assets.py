"""report_assets

Revision ID: 2d1004b03d75
Revises: 39a8686500c8
Create Date: 2026-08-08 09:21:59.739211
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '2d1004b03d75'
down_revision: str | None = '39a8686500c8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.UUID(), nullable=False),
        sa.Column("mime", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_report_assets_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_report_assets_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_assets")),
    )
    op.create_index(
        op.f("ix_report_assets_repository_id"), "report_assets", ["repository_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_report_assets_repository_id"), table_name="report_assets")
    op.drop_table("report_assets")
