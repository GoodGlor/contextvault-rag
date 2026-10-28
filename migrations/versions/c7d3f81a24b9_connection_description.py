"""a connection describes itself

Adds the generated description (#202) and the fingerprint of the allow-list it
was written from. Both default to the empty string rather than NULL: a
connection with no description is normal and fully usable, not an error state,
and every existing row is exactly that.

Revision ID: c7d3f81a24b9
Revises: b41c7a9e5d02
Create Date: 2026-08-14 12:41:05.882014
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'c7d3f81a24b9'
down_revision: str | None = 'b41c7a9e5d02'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "database_connections",
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "database_connections",
        sa.Column(
            "description_schema_hash", sa.String(length=64), nullable=False, server_default=""
        ),
    )


def downgrade() -> None:
    op.drop_column("database_connections", "description_schema_hash")
    op.drop_column("database_connections", "description")
