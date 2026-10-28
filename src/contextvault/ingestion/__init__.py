"""Ingestion pipeline: parse → chunk → embed → store.

v1 implements the parse and chunk stages; embedding/storage land in later cards.
"""

from contextvault.ingestion.chunking import TextChunk, chunk_document
from contextvault.ingestion.parsing import (
    IMAGE_SUFFIXES,
    DocumentError,
    DocumentParseError,
    ParsedDocument,
    TextBlock,
    UnsupportedDocumentError,
    file_suffix,
    parse_document,
    parsed_from_text,
)

__all__ = [
    "IMAGE_SUFFIXES",
    "DocumentError",
    "DocumentParseError",
    "ParsedDocument",
    "TextBlock",
    "TextChunk",
    "UnsupportedDocumentError",
    "chunk_document",
    "file_suffix",
    "parse_document",
    "parsed_from_text",
]
