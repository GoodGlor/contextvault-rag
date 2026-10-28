# Deploying ContextVault to Heroku

ContextVault ships as **one container** serving both halves: the built SPA and
the FastAPI API behind it, on a single origin. That matches what
`frontend/src/api/client.ts` already assumes for production — it prefixes every
call with `/api`, which `src/contextvault/spa.py` strips before routing, exactly
as the Vite dev proxy does locally.

Nothing in the image is Heroku-specific beyond honouring `$PORT`, so the same
`Dockerfile` runs on a VPS or any container host. Platform differences live in
config vars.

## What it costs

| Resource | Plan | Why this one |
| --- | --- | --- |
| Dyno | **Basic**, $7/mo | Must not sleep. `main.py` runs `scheduler_loop()` in the app lifespan, polling every 60s for due report schedules — an Eco dyno asleep at the scheduled time silently never fires it. |
| Postgres | **Essential-1**, $9/mo | 10 GB and 40 connections. Everything lives in Postgres: 1024-dim vectors, extracted document text, *and* report PDFs as `bytea` (`models/report.py`). Essential-0 (1 GB) works but needs periodic pruning of `report.pdf_data`. |
| **Total** | **$16/mo** | |

## Prerequisites

```bash
brew tap heroku/brew && brew install heroku
heroku login          # interactive — opens a browser
```

## First deploy

```bash
# 1. Create the app on the container stack (heroku.yml drives the build).
heroku create contextvault --stack=container

# 2. Postgres with pgvector. The extension is created by migration 0001.
heroku addons:create heroku-postgresql:essential-1 --app contextvault

# 3. Secrets. Neither has a usable default: SECRET_KEY must be >=32 bytes, and
#    crypto.py refuses to store a provider key rather than persist it in
#    plaintext if ENCRYPTION_KEY is unset.
heroku config:set --app contextvault \
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" \
  ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" \
  DATABASE_SSL=true \
  ENVIRONMENT=production

# 4. Deploy. Heroku builds the Dockerfile, runs `alembic upgrade head` in the
#    release phase, then swaps the dyno.
git push heroku main

# 5. Scale to the non-sleeping tier.
heroku ps:type web=basic --app contextvault

# 6. Create the first admin.
heroku run 'python -m contextvault.cli create-admin' --app contextvault
```

Deploying from a feature branch instead: `git push heroku <branch>:main`.

## Config vars

| Var | Value | Notes |
| --- | --- | --- |
| `DATABASE_URL` | *(set by the addon)* | Heroku injects the legacy `postgres://` scheme. A validator in `core/config.py` rewrites it to `postgresql+asyncpg://`, so the app and Alembic both get the async driver they require. Do not set this by hand. |
| `DATABASE_SSL` | `true` | Heroku Postgres refuses plaintext. Turns on an encrypted connection with chain verification disabled — its certificate does not validate against the system trust store. |
| `SECRET_KEY` | random, ≥32 bytes | JWT signing. |
| `ENCRYPTION_KEY` | Fernet key | Encrypts per-repository provider API keys at rest. **Losing it makes every stored provider key undecryptable.** Back it up. |
| `SPA_DIST_DIR` | *(baked into the image)* | `/app/frontend/dist`. Only set it to override. |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | default `5` / `5` | 10 connections per dyno. Raise only with the plan's cap in mind (Essential-0: 20, Essential-1: 40). |

Per-repository LLM provider keys are **not** config vars — they are entered in
the admin UI and stored encrypted in the database.

## Verifying a deploy

```bash
curl https://contextvault-<suffix>.herokuapp.com/health        # {"status":"ok"}
curl https://contextvault-<suffix>.herokuapp.com/api/health    # same, via the SPA's prefix
heroku logs --tail --app contextvault
```

Then open the app root — the SPA shell should load and a deep link such as
`/repositories/<id>` should resolve rather than 404.

**A 200 on its own proves nothing here.** The SPA catch-all answers *every*
unrecognised path with `index.html` and a 200 — including a misspelling like
`/healthz`, which is not a route in this app and still comes back "healthy".
Check the body, not the status, and for anything beyond "the process booted",
exercise the behaviour on the dyno (`heroku run python -c …` asserting the new
rule). A rendering change in particular fails as *a working report in the wrong
typeface*, which no probe of any URL can see.

## Testing the image locally

```bash
docker build -t contextvault:test .
docker run --rm -p 9099:9099 -e PORT=9099 \
  -e SECRET_KEY=local-smoke-test-key-at-least-32-bytes-long contextvault:test
```

`/health`, `/api/health`, `/`, and a deep link should all return 200 without a
database (routes needing Postgres will not).

## Known limits

- **Report generation is in-process.** `api/reports.py` uses FastAPI
  `BackgroundTasks`, and Heroku restarts dynos roughly daily. A report being
  generated at restart is orphaned in its current status. Scheduled reports
  recover on the next tick; a manually requested one has to be re-requested.
- **One dyno only.** `scheduler_loop()` runs in every web process, so a second
  dyno would double-run due schedules. Scaling out needs the scheduler moved to
  its own process type first.
