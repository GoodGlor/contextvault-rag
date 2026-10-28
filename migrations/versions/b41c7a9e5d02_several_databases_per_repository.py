"""several databases per repository

Drops the one-connection-per-repository constraint (#201) and gives each
connection a name that is unique within its repository.

The backfill uses the ``database`` value, which is the only human-meaningful
string already on the row. It cannot collide: the constraint it replaces
guaranteed at most one connection per repository, so every backfilled name is
alone in its repository by construction.

Revision ID: b41c7a9e5d02
Revises: 2d1004b03d75
Create Date: 2026-08-14 09:12:44.108233
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'b41c7a9e5d02'
down_revision: str | None = '2d1004b03d75'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "database_connections",
        sa.Column("name", sa.String(length=255), nullable=True),
    )
    op.execute("UPDATE database_connections SET name = database WHERE name IS NULL")
    op.alter_column("database_connections", "name", nullable=False)

    op.drop_constraint(
        "uq_database_connections_repository_id",
        "database_connections",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_database_connections_repository_name",
        "database_connections",
        ["repository_id", "name"],
    )
    op.create_index(
        op.f("ix_database_connections_repository_id"),
        "database_connections",
        ["repository_id"],
    )


def downgrade() -> None:
    # Downgrading re-imposes one connection per repository, so a repository
    # holding several cannot go back — the constraint would be violated by rows
    # this migration made legal. Fail loudly here rather than let
    # `create_unique_constraint` raise a bare IntegrityError.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM ("
                "  SELECT repository_id FROM database_connections"
                "  GROUP BY repository_id HAVING count(*) > 1"
                ") AS d"
            )
        )
        .scalar()
    )
    if duplicates:
        raise RuntimeError(
            f"{duplicates} repository(ies) hold more than one database connection; "
            "downgrading would re-impose one per repository. Remove the extras first."
        )

    op.drop_index(op.f("ix_database_connections_repository_id"), "database_connections")
    op.drop_constraint(
        "uq_database_connections_repository_name",
        "database_connections",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_database_connections_repository_id",
        "database_connections",
        ["repository_id"],
    )
    op.drop_column("database_connections", "name")
