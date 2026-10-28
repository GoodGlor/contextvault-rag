"""db_reports_tables

Revision ID: b8c2d5e7f901
Revises: f333a95e2154
Create Date: 2026-07-23 16:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b8c2d5e7f901'
down_revision: str | None = 'f333a95e2154'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "database_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("db_type", sa.Enum("postgres", "mysql", name="database_type"), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(255), nullable=False),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("password_encrypted", sa.String(1024), nullable=False),
        sa.Column("exposed_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id", name="uq_database_connections_repository_id"),
    )

    op.create_table(
        "report_schedules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("frozen_sql", sa.Text(), nullable=False),
        sa.Column("frozen_chart_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("run_at_time", sa.Time(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["database_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_report_schedules_repository_id", "report_schedules", ["repository_id"])

    op.create_table(
        "generated_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("generated_sql", sa.Text(), nullable=True),
        sa.Column("chart_spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "processing", "done", "failed", name="report_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("pdf_data", sa.LargeBinary(), nullable=True),
        sa.Column("pdf_filename", sa.String(255), nullable=True),
        sa.Column("schedule_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connection_id"], ["database_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["schedule_id"], ["report_schedules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generated_reports_repository_id", "generated_reports", ["repository_id"])
    op.create_index("ix_generated_reports_requested_by", "generated_reports", ["requested_by"])


def downgrade() -> None:
    op.drop_table("generated_reports")
    op.drop_table("report_schedules")
    op.drop_table("database_connections")
    op.execute("DROP TYPE report_status")
    op.execute("DROP TYPE database_type")
