"""Retrieval layer: access-filtered vector search over stored chunks.

``search_chunks`` is the raw SQL query (the access boundary); ``retrieve`` is
the service above it that embeds a question and applies a relevance threshold.
"""

from contextvault.retrieval.search import RetrievedChunk, search_chunks
from contextvault.retrieval.service import RetrievalResult, retrieve

__all__ = ["RetrievalResult", "RetrievedChunk", "retrieve", "search_chunks"]
