"""hnsw ann index on chunks embedding

Adds the pgvector ANN index the core schema (#3) deferred, so access-filtered
similarity search (#13) can use an index instead of a sequential scan. HNSW with
``vector_cosine_ops`` matches the cosine distance the retrieval query orders by.

Revision ID: b6be69ab221b
Revises: 1c6015cebe15
Create Date: 2026-07-08 12:17:36.854044
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b6be69ab221b"
down_revision: str | None = "1c6015cebe15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding_hnsw", table_name="chunks")
