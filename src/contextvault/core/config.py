"""Application settings, loaded from environment / .env."""

import os
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Which .env file to load, resolved at import time. Defaults to ``.env``; set
# ``CONTEXTVAULT_ENV_FILE`` to point elsewhere, or to an empty string to disable
# .env loading entirely. Tests set it empty so a developer's local .env never bleeds
# into the suite (settings then come only from real env vars + the defaults below).
_ENV_FILE = os.getenv("CONTEXTVAULT_ENV_FILE", ".env") or None


class Settings(BaseSettings):
    """Runtime configuration sourced from environment variables or a local .env."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "ContextVault"
    environment: str = "development"

    # The wall clock a nightly schedule's `run_at_time` is written in. Stored
    # times are naive — a reader typed "02:00" meaning their own two o'clock —
    # and until #164 they were compared against UTC, so a Ukrainian schedule
    # fired at 04:00 or 05:00 local depending on the season.
    #
    # An app-wide setting rather than a per-schedule column: one deployment, one
    # country, and this preserves what every stored value already means, so no
    # migration rewrites anybody's schedule. A second region would make this the
    # default and add the column beside it.
    schedule_timezone: str = "Europe/Kyiv"
    database_url: str = "postgresql+asyncpg://contextvault:contextvault@localhost:5432/contextvault"
    secret_key: str = "change-me-in-production"

    # Managed Postgres (Heroku, Neon, RDS…) requires TLS, and its certificate
    # chain is typically not verifiable against the system trust store, so
    # verification is disabled while encryption stays on (see db/session.py).
    # Off by default: the local docker-compose database speaks plaintext.
    database_ssl: bool = False

    # Connection-pool bounds, sized against the managed plan's connection cap
    # (Heroku Essential-0 allows 20, Essential-1 allows 40). Defaults total 10
    # per process, leaving headroom for a second dyno and the release-phase
    # migration. ``pool_pre_ping`` (always on) discards connections a managed
    # provider has reaped, which is routine on Heroku.
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # Directory holding the built SPA (``frontend/dist``). When set and present,
    # the API also serves the front end, so one dyno serves both on one origin —
    # which is what frontend/src/api/client.ts already assumes for production.
    # Unset in development, where Vite serves the SPA and proxies /api.
    spa_dist_dir: str | None = None

    # Embedding model, served by Google's Gemini embedding API (no local model
    # runs on the host). Handles Russian/Ukrainian + English via the asymmetric
    # ``task_type`` (document vs. query) the provider passes per call.
    embedding_model: str = "gemini-embedding-001"

    # Dimension of the pgvector embedding column. Gemini is asked for this width
    # via ``output_dimensionality`` on every call, so it need not match the
    # model's native output size; changing it still requires a re-embed and a
    # schema migration.
    embedding_dim: int = 1024

    # Gemini rejects embedding calls with 429 RESOURCE_EXHAUSTED once a per-minute
    # quota is spent, which a large document reaches easily: a multi-megabyte legal
    # code chunks into thousands of windows, i.e. dozens of requests.
    #
    # The free tier's ceiling is a *request* count, not a token count —
    # ``EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier``, 100
    # requests/minute. It is metered per project, so every concurrent ingestion and
    # every query-time embedding draws on the same budget. The default below stays
    # well under 100 to leave that headroom; raise it on a paid tier, or set 0 to
    # disable pacing. Pacing is process-wide (see ``embeddings.gemini``), so this is
    # a total rate, not a per-upload one.
    embedding_requests_per_minute: int = 50
    # A 429 is still transient, so retry rather than failing the whole ingest: the
    # backoff is exponential from ``embedding_retry_base_delay``, capped at
    # ``embedding_retry_max_delay``, and Gemini's own ``retryDelay`` (typically ~59s,
    # the remainder of the current minute) wins when present.
    embedding_max_retries: int = 6
    embedding_retry_base_delay: float = 2.0
    embedding_retry_max_delay: float = 60.0

    # Chunking (ingestion `chunk` stage). Character-based windows sized for
    # retrieval; ``chunk_overlap`` chars are shared between neighbours so a
    # passage split across a boundary still lands whole in some chunk. Must
    # satisfy ``chunk_overlap < chunk_size``.
    # Sized against the embedding quota as much as retrieval: Gemini meters embedding
    # by *request*, batched 100 chunks at a time, so chunk count drives quota spend
    # directly. At 3000/300 a multi-megabyte legal code costs roughly a third of the
    # requests it did at 1000/150, and 3000 chars of Cyrillic (~1000-1500 tokens)
    # still sits well inside the model's 2048-token input limit. The cost is coarser
    # retrieval: a citation points at a larger passage.
    chunk_size: int = 3000
    chunk_overlap: int = 300

    # Retrieval (RAG loop). Number of most-similar chunks the vector search
    # returns for a query, before generation. Higher gives the model more
    # context at the cost of prompt size / latency.
    retrieval_top_k: int = 5

    # Minimum cosine similarity (in ``[-1, 1]``) a retrieved chunk must reach to
    # count as relevant. Hits below this are dropped, so a query that finds only
    # weak matches yields no chunks — the signal behind the honest "not in this
    # vault" answer and the knowledge-gap dashboard. Tune per
    # embedding model; higher is stricter. This is a conservative default carried
    # over from the previous local model; Gemini's embedding distribution may
    # warrant re-tuning.
    retrieval_min_score: float = 0.3

    # Generation (RAG loop). ``llm_provider`` selects the system-default LLM
    # provider the RAG loop generates with; full per-repo
    # routing across providers is a later card. Each repository supplies its own
    # provider API key (encrypted at rest — see ``models/repository.py``); there
    # is no process-wide provider-key fallback, so no ``*_api_key`` settings. The
    # ``*_model`` values are only default model ids used when a repo omits one.
    # ``llm_max_tokens`` caps the generated answer length.
    llm_provider: str = "gemini"
    gemini_model: str = "gemini-2.5-flash"
    anthropic_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-4o"
    # OpenRouter is OpenAI-compatible: the same wire format reached through its
    # gateway. Model ids are vendor-namespaced (e.g. ``openai/gpt-4o``,
    # ``anthropic/claude-3.5-sonnet``); ``openrouter_base_url`` is the OpenAI
    # SDK's ``base_url`` override that points the client at the gateway.
    openrouter_model: str = "openai/gpt-4o"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_max_tokens: int = 2048

    # Master key for encrypting provider API keys at rest (see core/crypto.py).
    # A Fernet key sourced from env/secrets, never committed. Unset by default:
    # encryption fails loudly rather than storing plaintext, so it must be set
    # before any provider key is persisted. Generate one with
    # ``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
    encryption_key: str | None = None

    # JWT session tokens.
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # Onboarding invitations. How long an issued invite link stays
    # valid before it expires; the admin may override per-invite at issue time.
    invite_expiry_hours: int = 72

    # First-admin bootstrap (see `python -m contextvault.cli create-admin`).
    initial_admin_username: str | None = None
    initial_admin_password: str | None = None

    @field_validator("schedule_timezone")
    @classmethod
    def _require_a_real_zone(cls, value: str) -> str:
        """Fail at startup rather than at two in the morning.

        A typo here does not raise anywhere: ``ZoneInfo`` would be constructed
        inside the scheduler's poll loop, whose whole design swallows per-tick
        failures so one bad tick cannot kill it. The schedule would simply never
        fire at the right hour and nothing would say why — so the mistake is
        caught where somebody is watching the process come up.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"schedule_timezone: {value!r} is not an IANA time zone name "
                "(for example 'Europe/Kyiv' or 'UTC')."
            ) from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Normalise a bare Postgres DSN onto the async driver this app uses.

        Managed providers inject ``DATABASE_URL`` with a driverless scheme —
        Heroku still emits the legacy ``postgres://``, others ``postgresql://``.
        Neither names a driver, and SQLAlchemy would build a *sync* engine (or
        reject the legacy scheme outright), which every engine here would then
        fail on: ``db/session.py`` and ``migrations/env.py`` both call
        ``create_async_engine``. Rewriting once here means no call site — app,
        Alembic release phase, or test fixture — has to care where the URL came
        from. An explicit driver (``postgresql+asyncpg://``) is left untouched.
        """
        for scheme in ("postgres://", "postgresql://"):
            if value.startswith(scheme):
                return f"postgresql+asyncpg://{value[len(scheme) :]}"
        return value


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
