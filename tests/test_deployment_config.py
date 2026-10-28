"""Deployment seams: managed-Postgres DSNs, engine options, and SPA serving.

These cover the pieces that only differ in production, where a mistake is
invisible locally and fatal on the platform — a driverless ``DATABASE_URL``, a
database that demands TLS, and the SPA's ``/api`` prefix with no Vite proxy to
strip it.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from contextvault.core.config import Settings
from contextvault.db.session import engine_kwargs
from contextvault.spa import ApiPrefixMiddleware, mount_spa


def _settings(**overrides: Any) -> Settings:
    """Settings built from explicit values only (no env/.env bleed)."""
    # `_env_file` is pydantic-settings' per-instance override, not a model field.
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


class TestDatabaseUrlNormalisation:
    @pytest.mark.parametrize(
        "given",
        [
            "postgres://u:p@host:5432/db",  # what Heroku injects
            "postgresql://u:p@host:5432/db",  # what most other providers inject
        ],
    )
    def test_driverless_dsn_gains_the_async_driver(self, given: str) -> None:
        assert _settings(database_url=given).database_url == (
            "postgresql+asyncpg://u:p@host:5432/db"
        )

    def test_explicit_async_driver_is_left_alone(self) -> None:
        url = "postgresql+asyncpg://u:p@host:5432/db"
        assert _settings(database_url=url).database_url == url

    def test_query_string_and_credentials_survive(self) -> None:
        given = "postgres://u:p%40ss@host:5432/db?sslmode=require"
        assert _settings(database_url=given).database_url == (
            "postgresql+asyncpg://u:p%40ss@host:5432/db?sslmode=require"
        )


class TestEngineKwargs:
    def test_plaintext_by_default_for_local_postgres(self) -> None:
        assert "connect_args" not in engine_kwargs(_settings())

    def test_ssl_context_encrypts_without_chain_verification(self) -> None:
        import ssl

        kwargs = engine_kwargs(_settings(database_ssl=True))
        context = kwargs["connect_args"]["ssl"]
        assert isinstance(context, ssl.SSLContext)
        # Managed providers present certificates the system store cannot verify.
        assert context.verify_mode is ssl.CERT_NONE
        assert context.check_hostname is False

    def test_pool_bounds_come_from_settings(self) -> None:
        kwargs = engine_kwargs(_settings(db_pool_size=3, db_max_overflow=2))
        assert (kwargs["pool_size"], kwargs["max_overflow"]) == (3, 2)
        assert kwargs["pool_pre_ping"] is True


class TestApiPrefixMiddleware:
    @pytest.fixture
    def client(self) -> AsyncClient:
        app = FastAPI()
        app.add_middleware(ApiPrefixMiddleware)

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_prefixed_call_reaches_the_router(self, client: AsyncClient) -> None:
        assert (await client.get("/api/health")).status_code == 200

    async def test_unprefixed_call_still_works(self, client: AsyncClient) -> None:
        """The existing suite calls routes bare; the strip must not break that."""
        assert (await client.get("/health")).status_code == 200

    async def test_prefix_is_stripped_only_at_the_start(self, client: AsyncClient) -> None:
        assert (await client.get("/nope/api/health")).status_code == 404


class TestMountSpa:
    def test_absent_directory_is_not_an_error(self, tmp_path: Path) -> None:
        """Development has no build; the app must still start."""
        assert mount_spa(FastAPI(), str(tmp_path / "missing")) is False
        assert mount_spa(FastAPI(), None) is False

    async def test_built_spa_is_served_and_deep_links_fall_back(self, tmp_path: Path) -> None:
        (tmp_path / "index.html").write_text("<title>ContextVault</title>")
        app = FastAPI()

        @app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        assert mount_spa(app, str(tmp_path)) is True
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # An API route registered before the mount still wins.
            assert (await client.get("/health")).status_code == 200
            # A client-side route resolves to the SPA shell, not a 404.
            deep = await client.get("/repositories/abc")
            assert deep.status_code == 200
            assert "ContextVault" in deep.text


def test_a_misspelt_schedule_timezone_stops_the_process_coming_up() -> None:
    """#164. The scheduler's poll loop swallows per-tick failures by design, so
    a `ZoneInfo` built in there would raise once a tick into a log nobody reads
    and the schedule would simply never fire at the right hour. Caught at
    construction instead, where somebody is watching."""
    with pytest.raises(ValidationError, match="schedule_timezone"):
        Settings(schedule_timezone="Europe/Kiev/Nope")


def test_the_setting_takes_any_real_zone_including_utc() -> None:
    """A deployment elsewhere changes one environment variable — which is the
    whole reason this is a setting and not a constant."""
    assert Settings(schedule_timezone="UTC").schedule_timezone == "UTC"
    assert Settings(schedule_timezone="America/New_York").schedule_timezone == "America/New_York"
