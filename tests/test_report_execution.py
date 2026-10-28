"""Execution layer: results come back; writes and slow queries die."""

from typing import Any

import pytest
from sqlalchemy.engine.url import make_url

from contextvault.core.config import get_settings
from contextvault.models import DatabaseType
from contextvault.services.report_execution import QueryExecutionError, run_validated_query


def _params() -> dict[str, Any]:
    url = make_url(get_settings().database_url)
    return {
        "db_type": DatabaseType.POSTGRES,
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "database": url.database or "",
        "username": url.username or "",
        "password": url.password or "",
    }


async def test_select_returns_columns_and_rows() -> None:
    result = await run_validated_query(**_params(), sql="SELECT 1 AS one, 'kyiv' AS city")
    assert result.columns == ["one", "city"]
    assert result.rows == [(1, "kyiv")]


async def test_write_is_refused_by_read_only_transaction() -> None:
    # Even if a write slipped every guardrail, the READ ONLY transaction kills it.
    with pytest.raises(QueryExecutionError):
        await run_validated_query(**_params(), sql="CREATE TABLE smuggled (id int)")


async def test_statement_timeout_kills_slow_queries() -> None:
    with pytest.raises(QueryExecutionError):
        await run_validated_query(**_params(), sql="SELECT pg_sleep(2)", timeout_ms=200)
