"""Run validated report SQL with belt-and-braces safety (layer 4).

The guardrails already vetted the SQL; this layer independently enforces a
READ ONLY transaction (always rolled back), a statement timeout, and per-call
engines so a reporting DB can never hold app resources.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from contextvault.models import DatabaseType
from contextvault.services.report_db import build_url


class QueryExecutionError(Exception):
    """The validated query failed at the database (timeout, refused, bad SQL for engine)."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]


async def run_validated_query(
    *,
    db_type: DatabaseType,
    host: str,
    port: int,
    database: str,
    username: str,
    password: str,
    sql: str,
    timeout_ms: int = 15_000,
) -> QueryResult:
    engine = create_async_engine(
        build_url(
            db_type=db_type,
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
        )
    )
    try:
        async with engine.connect() as conn:
            if db_type is DatabaseType.POSTGRES:
                # First statement of the autobegun transaction — READ ONLY applies to it.
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_ms)}"))
            else:
                await conn.execute(text("SET SESSION transaction_read_only = 1"))
                await conn.execute(text(f"SET SESSION max_execution_time = {int(timeout_ms)}"))
            result = await conn.execute(text(sql))
            columns = list(result.keys())
            rows = [tuple(row) for row in result.all()]
            await conn.rollback()
            return QueryResult(columns=columns, rows=rows)
    except QueryExecutionError:
        raise
    except Exception as exc:  # noqa: BLE001 — driver errors vary; one clean failure type
        raise QueryExecutionError(f"Query failed: {exc}") from exc
    finally:
        await engine.dispose()
