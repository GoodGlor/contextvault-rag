"""Connector round-trips against the test Postgres itself as the 'external' DB."""

from typing import Any

import pytest
from sqlalchemy.engine.url import make_url

from contextvault.core.config import get_settings
from contextvault.models import DatabaseType
from contextvault.services.report_db import DBConnectionError, introspect_schema, test_connection

# ``test_connection`` is a *function under test*, not a test case, but pytest's
# default collection matches anything named ``test_*`` in a test module — even
# imported symbols. Without this, pytest tries to collect it directly and fails
# looking for fixtures named after its keyword-only params (db_type, host, ...).
test_connection.__test__ = False  # type: ignore[attr-defined]


def _own_db_params() -> dict[str, Any]:
    url = make_url(get_settings().database_url)
    return {
        "db_type": DatabaseType.POSTGRES,
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "database": url.database or "",
        "username": url.username or "",
        "password": url.password or "",
    }


async def test_connection_succeeds_against_real_db() -> None:
    await test_connection(**_own_db_params())  # must not raise


async def test_connection_bad_password_raises() -> None:
    params = {**_own_db_params(), "password": "definitely-wrong", "username": "nobody"}
    with pytest.raises(DBConnectionError):
        await test_connection(**params)


async def test_introspect_lists_own_tables() -> None:
    schema = await introspect_schema(**_own_db_params())
    tables = {t["table"] for t in schema}
    assert "repositories" in tables  # our own schema is visible
    repo_table = next(t for t in schema if t["table"] == "repositories")
    assert {"name": "name", "description": ""} in repo_table["columns"]
    assert all(t["description"] == "" for t in schema)
