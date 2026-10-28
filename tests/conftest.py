"""Shared pytest fixtures.

The ``db_session`` fixture yields an ``AsyncSession`` bound to a connection whose
outer transaction is rolled back after each test, so integration tests never
leave data behind. It skips (rather than fails) when no migrated database is
reachable, keeping the pure-unit suite green in environments without Postgres.
"""

import os

from cryptography.fernet import Fernet

# Ignore the developer's local .env during tests: settings come only from real
# environment variables + code defaults, so a local override (e.g. OPENROUTER_MODEL)
# can never bleed into the suite. Must be set before contextvault.core.config is
# imported (it resolves the env-file choice at import time).
os.environ["CONTEXTVAULT_ENV_FILE"] = ""

# A >=32-byte secret keeps PyJWT from emitting InsecureKeyLength warnings during
# tests. Set before any settings are read. Production overrides via the env/.env.
os.environ.setdefault("SECRET_KEY", "test-secret-key-that-is-at-least-32-bytes")

# A valid Fernet master key so the crypto module can encrypt/decrypt in tests.
# Production supplies its own via ENCRYPTION_KEY; this per-run key never persists.
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

from collections.abc import AsyncGenerator  # noqa: E402

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.exc import DBAPIError, OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

import contextvault.models  # noqa: E402, F401  (registers tables on Base.metadata)
from contextvault.core.config import get_settings  # noqa: E402
from contextvault.db.base import Base  # noqa: E402


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as check:
            await check.execute(sa.text("SELECT 1 FROM users LIMIT 0"))
    except (OperationalError, DBAPIError) as exc:  # unreachable or unmigrated
        await engine.dispose()
        pytest.skip(f"no migrated database available: {exc.__class__.__name__}")

    conn = await engine.connect()
    trans = await conn.begin()
    # Start each test from a clean slate regardless of any committed rows; the
    # outer rollback below restores them, so the clearing is test-local only.
    # DELETE (not TRUNCATE) deliberately: TRUNCATE takes an ACCESS EXCLUSIVE lock
    # that would sit for the whole test (only released by the final rollback),
    # blocking any other connection to the same tables — e.g. report generation's
    # own per-call reporting-DB connection (services/report_execution.py) querying
    # the app DB in tests. DELETE takes a ROW EXCLUSIVE lock, which does not block
    # concurrent readers. No table here has a sequence (all PKs are app-generated
    # UUIDs), so there is no identity counter to restart.
    for table in reversed(Base.metadata.sorted_tables):
        await conn.execute(table.delete())
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        if trans.is_active:
            await trans.rollback()
        await conn.close()
        await engine.dispose()
