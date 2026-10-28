# ContextVault — developer task runner.
#
# `make test` runs the full CI-parity gate (backend + frontend) — the same checks
# .github/workflows/ci.yml runs, so a green `make test` locally means green CI.
# Run `make help` for the individual targets.
#
# Backend commands go through `uv`; frontend commands through npm in ./frontend.

FRONTEND := frontend
NPM := npm --prefix $(FRONTEND)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---- Aggregate ------------------------------------------------------------

.PHONY: test
test: test-backend test-frontend ## Full CI-parity gate: backend + frontend (what CI runs)

.PHONY: ci
ci: test ## Alias for `test`

# ---- Backend (mirrors the CI "Lint, type-check, tests" job) ---------------

# WeasyPrint reaches Pango through cffi's dlopen, which does not search Homebrew's
# prefix: on macOS the import dies inside dlopen looking for gobject, naming
# neither Pango nor Homebrew. Inert on Linux, where the loader finds them anyway.
# `brew install pango` is the other half. The prefix is asked
# for rather than hardcoded — it is /usr/local on an Intel Mac, where the literal
# /opt/homebrew silently does nothing and the failure looks like a missing library.
BREW_PREFIX := $(shell brew --prefix 2>/dev/null || echo /opt/homebrew)
PYTEST := DYLD_FALLBACK_LIBRARY_PATH=$(BREW_PREFIX)/lib uv run pytest

.PHONY: test-backend
test-backend: ## Backend gate: ruff check + format-check + mypy + migrations + pytest
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy
	uv run alembic upgrade head
	$(PYTEST)

.PHONY: test-backend-fast
test-backend-fast: ## Just the backend tests (pytest), skipping lint/type/migrate
	$(PYTEST)

# ---- Frontend (mirrors the CI "Frontend ..." job) -------------------------

.PHONY: test-frontend
test-frontend: ## Frontend gate: lint + format-check + typecheck + vitest + build
	$(NPM) run lint
	$(NPM) run format:check
	$(NPM) run typecheck
	$(NPM) test
	$(NPM) run build

.PHONY: test-frontend-fast
test-frontend-fast: ## Just the frontend unit tests (vitest)
	$(NPM) test

# ---- End-to-end (Playwright; NOT in CI) -----------------------------------

.PHONY: test-e2e
test-e2e: ## Playwright e2e — bring the stack up first (./dev.sh); needs a verified Gemini key
	$(NPM) run test:e2e

# ---- Convenience ----------------------------------------------------------

.PHONY: db
db: ## Start Postgres (docker compose) and apply migrations
	docker compose up -d
	uv run alembic upgrade head

.PHONY: fmt
fmt: ## Auto-format: backend (ruff) + frontend (prettier)
	uv run ruff format src tests
	$(NPM) run format
