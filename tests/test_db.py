"""Tests for the database base metadata."""

from contextvault.db.base import Base


def test_metadata_uses_naming_convention() -> None:
    convention = Base.metadata.naming_convention
    assert set(convention) == {"ix", "uq", "ck", "fk", "pk"}
    assert convention["pk"] == "pk_%(table_name)s"
