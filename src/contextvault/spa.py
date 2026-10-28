"""Serving the built SPA from the API process (single-origin production).

In development Vite serves the front end and proxies ``/api`` to this backend,
stripping the prefix on the way (see ``frontend/vite.config.ts``). In production
there is no Vite: one process serves both, so this module reproduces that same
contract — strip ``/api``, then fall back to the SPA's ``index.html`` for any
path the API did not claim.

Doing the strip in middleware, rather than mounting every router under an
``/api`` prefix, keeps the routes at the paths the whole test suite already
calls (``/auth/login``, ``/health``, …).
"""

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

API_PREFIX = "/api"


class ApiPrefixMiddleware:
    """Strip a leading ``/api`` so the SPA's prefixed calls reach the routers.

    ``frontend/src/api/client.ts`` prefixes every request with ``/api``. The
    Vite dev proxy rewrites that away before forwarding; this is the production
    equivalent. Paths without the prefix pass through untouched, which is why
    the existing tests — which call the routers directly at ``/auth/login`` and
    friends — are unaffected.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path: str = scope["path"]
            if path == API_PREFIX or path.startswith(f"{API_PREFIX}/"):
                scope = dict(scope)
                scope["path"] = path[len(API_PREFIX) :] or "/"
                # ``raw_path`` would still carry the prefix; Starlette prefers it
                # in some code paths, so drop it rather than leave it disagreeing
                # with ``path``.
                scope.pop("raw_path", None)
        await self.app(scope, receive, send)


class SpaStaticFiles(StaticFiles):
    """Static files that fall back to ``index.html`` for unknown paths.

    The SPA routes client-side, so a deep link (``/repositories/<id>``) is a real
    URL to the browser but not a file on disk. Serving ``index.html`` instead of
    404 lets the router resolve it after hydration.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        # A missing file arrives as a raised 404, not a 404 response, so both
        # shapes have to be caught for the fallback to fire.
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)
        if response.status_code == 404:
            return await super().get_response("index.html", scope)
        return response


def mount_spa(app: FastAPI, dist_dir: str | None) -> bool:
    """Mount the built SPA at ``/`` if it is present. Returns whether it mounted.

    Missing or unbuilt is not an error: in development the directory does not
    exist and Vite owns the front end. The mount goes on last so every API route
    registered before it still wins.
    """
    if dist_dir is None:
        return False
    dist = Path(dist_dir)
    if not (dist / "index.html").is_file():
        return False
    app.mount("/", SpaStaticFiles(directory=dist, html=True), name="spa")
    return True
