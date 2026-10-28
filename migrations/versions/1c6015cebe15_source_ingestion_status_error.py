"""source ingestion status + error

Revision ID: 1c6015cebe15
Revises: c800a89250e1
Create Date: 2026-07-08 09:31:23.047638
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '1c6015cebe15'
down_revision: str | None = 'c800a89250e1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Unlike create_table, add_column does NOT auto-create a PostgreSQL ENUM type,
# so create it explicitly here (and drop it on downgrade). create_type=False on
# the column keeps SQLAlchemy from trying to create it a second time.
source_status = postgresql.ENUM(
    'pending', 'processing', 'done', 'failed', name='source_status', create_type=False
)


def upgrade() -> None:
    source_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'sources',
        sa.Column('status', source_status, server_default='pending', nullable=False),
    )
    op.add_column('sources', sa.Column('ingest_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('sources', 'ingest_error')
    op.drop_column('sources', 'status')
    op.execute('DROP TYPE IF EXISTS source_status')
