"""repository llm config

Revision ID: ed2189c1cf01
Revises: b6be69ab221b
Create Date: 2026-07-10 10:00:13.366468
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'ed2189c1cf01'
down_revision: str | None = 'b6be69ab221b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create the native enum type explicitly (checkfirst) rather than relying on
    # add_column's implicit CREATE TYPE, which is unreliable for a standalone
    # column add. create_type=False on the column then references it by name.
    llm_provider = sa.Enum(
        "gemini", "openai", "openrouter", "anthropic", name="llm_provider"
    )
    llm_provider.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "repositories",
        sa.Column(
            "llm_provider",
            sa.Enum(
                "gemini", "openai", "openrouter", "anthropic",
                name="llm_provider",
                create_type=False,
            ),
            nullable=True,
        ),
    )
    op.add_column("repositories", sa.Column("llm_model", sa.String(length=255), nullable=True))
    op.add_column("repositories", sa.Column("api_key_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("repositories", "api_key_encrypted")
    op.drop_column("repositories", "llm_model")
    op.drop_column("repositories", "llm_provider")
    # The enum type is not dropped with its column; drop explicitly so a
    # re-upgrade doesn't hit "type already exists" (mirrors the core schema).
    op.execute("DROP TYPE IF EXISTS llm_provider")
