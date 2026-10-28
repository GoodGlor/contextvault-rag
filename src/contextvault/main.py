"""FastAPI application entrypoint."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI

from contextvault.api.analytics import router as analytics_router
from contextvault.api.auth import router as auth_router
from contextvault.api.conversations import router as conversation_router
from contextvault.api.database import router as database_router
from contextvault.api.grants import router as grants_router
from contextvault.api.health import router as health_router
from contextvault.api.invitations import router as invitations_router
from contextvault.api.knowledge_gaps import router as knowledge_gaps_router
from contextvault.api.providers import router as providers_router
from contextvault.api.query import router as query_router
from contextvault.api.report_assets import router as report_assets_router
from contextvault.api.report_fonts import router as report_fonts_router
from contextvault.api.report_schedules import router as report_schedules_router
from contextvault.api.reports import router as reports_router
from contextvault.api.repositories import router as repositories_router
from contextvault.api.sources import router as sources_router
from contextvault.api.users import router as users_router
from contextvault.core.config import get_settings
from contextvault.services.report_scheduler import scheduler_loop
from contextvault.spa import ApiPrefixMiddleware, mount_spa


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the nightly report scheduler for the life of the app process.

    Note: httpx's ``ASGITransport`` (used throughout the test suite) does not
    invoke the lifespan, so the scheduler task never starts during tests.
    """
    task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    # Outermost: rewrite /api/* onto the router paths before anything routes.
    app.add_middleware(ApiPrefixMiddleware)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(invitations_router)
    app.include_router(users_router)
    app.include_router(sources_router)
    app.include_router(database_router)
    app.include_router(repositories_router)
    app.include_router(providers_router)
    app.include_router(grants_router)
    app.include_router(query_router)
    app.include_router(conversation_router)
    app.include_router(knowledge_gaps_router)
    app.include_router(analytics_router)
    app.include_router(reports_router)
    app.include_router(report_schedules_router)
    app.include_router(report_assets_router)
    app.include_router(report_fonts_router)
    # Last, so the catch-all SPA mount never shadows an API route.
    mount_spa(app, settings.spa_dist_dir)
    return app


app = create_app()
