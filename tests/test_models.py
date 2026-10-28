"""Schema-level tests for the core ORM models.

These assert the table/column/constraint shape on ``Base.metadata`` without a
live database, so they run fast and deterministically. Migration application
against a real pgvector database is verified separately (see the PR checks).
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# Importing the package registers every model on Base.metadata.
import contextvault.models  # noqa: F401
from contextvault.core.config import get_settings
from contextvault.db.base import Base
from contextvault.models import LLMProviderName, Repository, SourceKind


def _table(name: str) -> sa.Table:
    assert name in Base.metadata.tables, f"missing table: {name}"
    return Base.metadata.tables[name]


def test_all_core_tables_registered() -> None:
    expected = {"users", "repositories", "sources", "chunks", "grants"}
    assert expected <= set(Base.metadata.tables)


def test_users_columns_and_constraints() -> None:
    users = _table("users")
    cols = users.columns
    assert {"id", "username", "password_hash", "role", "must_change_password"} <= set(cols.keys())
    assert users.c.username.unique is True
    assert users.c.password_hash.nullable is False
    assert users.c.must_change_password.nullable is False
    # role is a native enum with the two v1 values
    role_type = users.c.role.type
    assert isinstance(role_type, sa.Enum)
    assert set(role_type.enums) == {"admin", "user"}


def test_grants_link_users_and_repositories_with_expiry() -> None:
    grants = _table("grants")
    referenced = {fk.column.table.name for fk in grants.foreign_keys}
    assert {"users", "repositories"} <= referenced
    assert "expires_at" in grants.columns
    assert grants.c.expires_at.nullable is True
    # a user may be granted a given repo at most once
    uniques = {
        tuple(sorted(col.name for col in c.columns))
        for c in grants.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert ("repository_id", "user_id") in uniques


def test_repositories_carry_llm_model_choice() -> None:
    """A repo picks a provider (native enum) + model — both nullable, since a repo
    starts with no model chosen. The API key is NOT here; it lives per-provider in
    ``provider_settings`` (design: global provider keys)."""
    repos = _table("repositories")
    cols = repos.columns
    assert {"llm_provider", "llm_model"} <= set(cols.keys())
    # The per-repo key is gone — keys are global now.
    assert "api_key_encrypted" not in cols

    provider_type = repos.c.llm_provider.type
    assert isinstance(provider_type, sa.Enum)
    assert set(provider_type.enums) == {"gemini", "openai", "openrouter", "anthropic", "custom"}

    # No model chosen by default: both columns are nullable.
    assert repos.c.llm_provider.nullable is True
    assert repos.c.llm_model.nullable is True


def test_repository_llm_selected_requires_provider_and_model() -> None:
    """``llm_selected`` is true once a provider and model are picked. It is not the
    full answerability predicate — that also needs the provider's global key (checked
    in the service layer), so the model here has no key field."""
    repo = Repository(name="Vault")
    assert repo.llm_selected is False

    repo.llm_provider = LLMProviderName.OPENAI
    assert repo.llm_selected is False
    repo.llm_model = "gpt-4o"
    assert repo.llm_selected is True


def test_provider_settings_table_holds_one_key_per_provider() -> None:
    """Global provider keys: one row per provider (unique), an encrypted key, and a
    ``verified_at`` stamp set when the key last passed its live check. The key is
    nullable so a custom (OpenAI-compatible) endpoint can be keyless — base URL only."""
    settings = _table("provider_settings")
    cols = settings.columns
    assert {"provider", "api_key_encrypted", "verified_at", "base_url"} <= set(cols.keys())
    assert cols["api_key_encrypted"].nullable is True
    assert cols["base_url"].nullable is True

    provider_type = settings.c.provider.type
    assert isinstance(provider_type, sa.Enum)
    assert set(provider_type.enums) == {"gemini", "openai", "openrouter", "anthropic", "custom"}

    uniques = {
        tuple(col.name for col in c.columns)
        for c in settings.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert ("provider",) in uniques


def test_sources_belong_to_repository_and_have_kind() -> None:
    sources = _table("sources")
    kind_type = sources.c.kind.type
    assert isinstance(kind_type, sa.Enum)
    assert set(kind_type.enums) == {"document", "admin_note", "image", "web"}
    repo_fk = [fk for fk in sources.c.repository_id.foreign_keys]
    assert repo_fk and repo_fk[0].column.table.name == "repositories"
    assert repo_fk[0].ondelete == "CASCADE"


def test_source_kinds_include_image_and_web() -> None:
    assert SourceKind.IMAGE.value == "image"
    assert SourceKind.WEB.value == "web"


def test_source_has_optional_source_url() -> None:
    from contextvault.models import Source

    src = Source(repository_id=None, kind=SourceKind.WEB, title="t", source_url="https://x.test")
    assert src.source_url == "https://x.test"


def test_chunks_carry_vector_and_denormalized_repository() -> None:
    """The repository_id on chunks lets the access filter + vector search run
    as a single SQL query."""
    chunks = _table("chunks")
    assert isinstance(chunks.c.embedding.type, Vector)
    assert chunks.c.embedding.type.dim == get_settings().embedding_dim
    # denormalized repository_id present alongside source_id
    assert "repository_id" in chunks.columns
    assert "source_id" in chunks.columns
    src_fk = [fk for fk in chunks.c.source_id.foreign_keys]
    assert src_fk and src_fk[0].ondelete == "CASCADE"
