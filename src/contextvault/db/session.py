"""Async database engine and request-scoped session dependency."""

import ssl
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from contextvault.core.config import Settings, get_settings


def engine_kwargs(settings: Settings | None = None) -> dict[str, Any]:
    """Engine options shared by the app engine and Alembic's migration engine.

    Both must agree, or the release-phase migration passes locally and then
    fails against a managed database that demands TLS. Keeping the options in
    one place is why ``migrations/env.py`` imports this rather than repeating
    them.

    ``pool_pre_ping`` is unconditional: managed providers reap idle
    connections, and a checked-out dead connection otherwise surfaces as a
    spurious 500 on the next request.
    """
    settings = settings or get_settings()
    kwargs: dict[str, Any] = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_pre_ping": True,
    }
    if settings.database_ssl:
        # Encrypt, but skip chain/hostname verification: Heroku Postgres (like
        # most managed Postgres) presents a certificate that does not validate
        # against the system trust store, so verifying here would refuse every
        # connection. asyncpg takes the context through ``connect_args``.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        kwargs["connect_args"] = {"ssl": context}
    return kwargs


_settings = get_settings()

engine: AsyncEngine = create_async_engine(_settings.database_url, **engine_kwargs(_settings))
SessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a request-scoped async session."""
    async with SessionLocal() as session:
        yield session
