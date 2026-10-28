"""Alembic migration environment (async)."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import contextvault.models  # noqa: F401  (registers all tables on Base.metadata)
from contextvault.core.config import get_settings
from contextvault.db.base import Base
from contextvault.db.session import engine_kwargs

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    url = get_settings().database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database using an async engine."""
    settings = get_settings()
    # Same TLS/pool options the app engine uses — the release phase talks to the
    # same managed database, which refuses a plaintext connection.
    connectable = create_async_engine(settings.database_url, **engine_kwargs(settings))
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
