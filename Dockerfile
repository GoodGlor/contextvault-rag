# syntax=docker/dockerfile:1
#
# One image serving both halves of ContextVault: the built SPA and the FastAPI
# API behind it, on a single origin (see src/contextvault/spa.py). Nothing here
# is Heroku-specific beyond honouring $PORT, so the same image runs on a VPS or
# any container host — platform differences stay in config vars.

# ---- Stage 1: build the SPA -------------------------------------------------
FROM node:22-slim AS spa

WORKDIR /spa
# Manifests first: dependencies re-install only when they actually change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: runtime -------------------------------------------------------
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# The image had no apt layer at all until report PDFs moved to WeasyPrint, which
# renders through Pango. libpangoft2-1.0-0 pulls pango, harfbuzz, freetype and
# fontconfig -- 17 MB in total. fonts-dejavu-core arrives with them regardless
# (fontconfig-config depends on it or fonts-liberation), but naming it keeps
# "Pango installed with no font at all" impossible rather than merely unlikely.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpangoft2-1.0-0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Headless chart rendering for report PDFs (services/report_render.py):
    # matplotlib must not look for a display.
    MPLBACKEND=Agg \
    SPA_DIST_DIR=/app/frontend/dist \
    PATH="/app/.venv/bin:$PATH"

# Dependencies in their own layer, before any source, so editing code does not
# reinstall the world. --no-dev drops ruff/mypy/pytest from the runtime image.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY src/ ./src/
RUN uv sync --frozen --no-dev

COPY --from=spa /spa/dist ./frontend/dist

# $PORT is assigned at runtime, so a shell is needed to expand it — but `exec`
# hands the process over to uvicorn so it receives SIGTERM directly. Without it
# the shell would swallow the signal and Heroku's dyno restart would kill the
# app mid-shutdown, skipping the lifespan cleanup in main.py.
CMD ["sh", "-c", "exec uvicorn contextvault.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
