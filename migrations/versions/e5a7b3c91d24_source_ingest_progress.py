"""source ingest progress

Embed-stage progress counters so the admin UI can show a real percentage while a
large document ingests. Nullable with no backfill: sources ingested before this
migration simply have no counters, and the UI falls back to the status badge.

Revision ID: e5a7b3c91d24
Revises: a9b0c1d2e3f4
Create Date: 2026-07-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a7b3c91d24'
down_revision: str | None = 'a9b0c1d2e3f4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sources", sa.Column("chunks_total", sa.Integer(), nullable=True))
    op.add_column("sources", sa.Column("chunks_done", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("sources", "chunks_done")
    op.drop_column("sources", "chunks_total")
