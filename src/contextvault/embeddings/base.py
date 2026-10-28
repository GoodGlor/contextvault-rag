"""The ``EmbeddingProvider`` abstraction.

Embeddings are pluggable. v1 ships a single local,
multilingual implementation, but the retrieval/ingestion code depends only on
this interface so a paid provider can be swapped in later without touching
callers. The pgvector column dimension is tied to the active model's
``dimension`` — changing models means re-embedding.
"""

from collections.abc import Sequence
from typing import Literal, Protocol, runtime_checkable

# Gemini's asymmetric retrieval task types: documents and queries embed differently.
# Declared here (not in the Gemini module) so the protocol and every implementer —
# Gemini-backed or fake — share one narrow type instead of each guessing at ``str``.
EmbedTask = Literal["document", "query"]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Turns text into fixed-length vectors for similarity search."""

    @property
    def dimension(self) -> int:
        """Length of every vector this provider returns."""
        ...

    def embed(self, texts: Sequence[str], *, task: EmbedTask = "document") -> list[list[float]]:
        """Embed ``texts`` into vectors, one per input, each of ``dimension``.

        ``task`` is ``"document"`` for stored content and ``"query"`` for a search
        query — providers that support asymmetric retrieval embeddings use it.
        """
        ...
