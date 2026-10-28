"""Chunk model — an embedded slice of a source used for retrieval."""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.core.config import get_settings
from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Chunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A retrievable passage with its embedding.

    ``repository_id`` is denormalized from the parent source so the access
    filter and the vector similarity search run as a single SQL query — the
    permission boundary lives in the query itself.
    """

    __tablename__ = "chunks"
    # HNSW ANN index for cosine similarity search — the ops class matches the
    # cosine distance the retrieval query orders by. Created by
    # migration b6be69ab221b; declared here so the ORM stays in sync.
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Position of this chunk within its source.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Character span into the source content, for citation highlighting.
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Populated by the ingestion pipeline; dimension tied to the embedding model.
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=True
    )
