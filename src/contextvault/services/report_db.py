"""Connect/introspect admin-supplied reporting databases.

Engines are created per call and disposed — connections are rare, admin-driven
events, not hot-path traffic. NOTE: unlike web sources there is deliberately no
public-host (SSRF) guard here — internal DB hosts are the feature; the boundary
is that only admins may create connections.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine.url import URL
from sqlalchemy.ext.asyncio import create_async_engine

from contextvault.models import DatabaseType

_DRIVERS = {DatabaseType.POSTGRES: "postgresql+asyncpg", DatabaseType.MYSQL: "mysql+aiomysql"}

# information_schema is shared SQL vocabulary: this one query introspects both engines.
_INTROSPECT_SQL = text(
    """
    SELECT table_name, column_name
    FROM information_schema.columns
    WHERE table_schema = :schema
    ORDER BY table_name, ordinal_position
    """
)


class DBConnectionError(Exception):
    """The reporting database could not be reached/queried with these details."""


def build_url(
    *, db_type: DatabaseType, host: str, port: int, database: str, username: str, password: str
) -> URL:
    """Assemble the SQLAlchemy URL for a reporting connection (never log it)."""
    return URL.create(
        _DRIVERS[db_type],
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    )


def _default_schema(db_type: DatabaseType, database: str) -> str:
    # Postgres user tables live in ``public``; MySQL's "schema" IS the database.
    return "public" if db_type is DatabaseType.POSTGRES else database


async def test_connection(
    *, db_type: DatabaseType, host: str, port: int, database: str, username: str, password: str
) -> None:
    """SELECT 1 against the target; raise :class:`DBConnectionError` on any failure."""
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
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — driver errors vary; one clean failure type
        raise DBConnectionError(f"Could not connect: {exc}") from exc
    finally:
        await engine.dispose()


async def introspect_schema(
    *, db_type: DatabaseType, host: str, port: int, database: str, username: str, password: str
) -> list[dict[str, Any]]:
    """Live tables/columns in exposed-schema shape (descriptions empty, for the admin to fill)."""
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
            rows = (
                await conn.execute(_INTROSPECT_SQL, {"schema": _default_schema(db_type, database)})
            ).all()
    except Exception as exc:  # noqa: BLE001
        raise DBConnectionError(f"Could not introspect: {exc}") from exc
    finally:
        await engine.dispose()
    tables: dict[str, list[dict[str, str]]] = {}
    for table_name, column_name in rows:
        tables.setdefault(table_name, []).append({"name": column_name, "description": ""})
    return [{"table": name, "description": "", "columns": cols} for name, cols in tables.items()]
