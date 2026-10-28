"""#212: the background worker that writes a connection's description.

``tests/test_database_api.py`` proves the *endpoint* queues this and does not
call a provider itself; this file proves what the task does once it runs, which
that module deliberately patches out — a detached task with its own session
cannot run inside an API test's event loop without leaking a connection into the
next one.
"""

import datetime
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import encrypt
from contextvault.llm.textgen import TextGenError
from contextvault.models import (
    DatabaseConnection,
    DatabaseType,
    LLMProviderName,
    ProviderSetting,
    Repository,
)
from contextvault.services import report_llm
from contextvault.services.connection_description import (
    description_is_current,
    run_connection_description,
)
from contextvault.services.report_llm import schema_fingerprint

SCHEMA = [
    {
        "table": "orders",
        "description": "Customer orders",
        "columns": [{"name": "total", "description": "Order total"}],
    }
]
WIDER = [
    {
        "table": "orders",
        "description": "Customer orders",
        "columns": [
            {"name": "total", "description": "Order total"},
            {"name": "city", "description": "Billing city"},
        ],
    }
]


def _fixed_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    """A session factory that always yields ``session`` without closing it."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


async def _connection(
    db_session: AsyncSession,
    *,
    answerable: bool = True,
    schema: list[dict[str, object]] | None = None,
) -> DatabaseConnection:
    if answerable:
        db_session.add(
            ProviderSetting(
                provider=LLMProviderName.OPENAI,
                api_key_encrypted=encrypt("sk-test"),
                verified_at=datetime.datetime.now(datetime.UTC),
            )
        )
    repo = Repository(
        name="Reporting Vault",
        llm_provider=LLMProviderName.OPENAI if answerable else None,
        llm_model="gpt-4o" if answerable else None,
    )
    db_session.add(repo)
    await db_session.flush()
    conn = DatabaseConnection(
        repository_id=repo.id,
        name="Reporting",
        db_type=DatabaseType.POSTGRES,
        host="db.internal",
        port=5432,
        database="reporting",
        username="readonly",
        password_encrypted=encrypt("pw"),
        exposed_schema=SCHEMA if schema is None else schema,
    )
    db_session.add(conn)
    await db_session.flush()
    return conn


def _replies(monkeypatch: pytest.MonkeyPatch, text: str) -> list[str]:
    prompts: list[str] = []

    async def fake_generate_text(*args: object, prompt: str = "", **kwargs: object) -> str:
        prompts.append(prompt)
        return text

    monkeypatch.setattr(report_llm, "generate_text", fake_generate_text)
    return prompts


class TestTheWorker:
    async def test_it_writes_a_description_and_stamps_the_allow_list(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = await _connection(db_session)
        prompts = _replies(monkeypatch, "Customer order totals.")

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == "Customer order totals."
        # Stamped, so the connection is not immediately stale — #202's rule.
        assert conn.description_schema_hash == schema_fingerprint(SCHEMA)
        # It described the ALLOW-LIST, not the live database.
        assert "Customer orders" in prompts[0]

    async def test_it_does_nothing_when_the_description_is_already_current(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two saves in quick succession queue two tasks; the second has nothing
        left to do, and must not spend a second provider call."""
        conn = await _connection(db_session)
        conn.description = "Already written."
        conn.description_schema_hash = schema_fingerprint(SCHEMA)
        await db_session.flush()
        prompts = _replies(monkeypatch, "Rewritten.")

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == "Already written."
        assert prompts == []

    async def test_a_changed_allow_list_is_described_again(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's case: a column added, so the old description describes
        something that no longer exists."""
        conn = await _connection(db_session)
        conn.description = "Written for the narrower list."
        conn.description_schema_hash = schema_fingerprint(SCHEMA)
        conn.exposed_schema = WIDER
        await db_session.flush()
        _replies(monkeypatch, "Orders with billing city.")

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == "Orders with billing city."
        assert conn.description_schema_hash == schema_fingerprint(WIDER)

    async def test_a_hand_written_description_is_replaced_when_the_schema_changes(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's ruling: auto, but still correctable. A correction is a
        correction *of this allow-list*, so it survives until the allow-list
        changes — not forever."""
        conn = await _connection(db_session)
        conn.description = "An admin's careful wording."
        conn.description_schema_hash = schema_fingerprint(SCHEMA)
        conn.exposed_schema = WIDER
        await db_session.flush()
        _replies(monkeypatch, "Machine wording.")

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == "Machine wording."

    async def test_a_provider_failure_leaves_everything_as_it_was(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#202's rule, unchanged: nothing about querying a database depends on
        it having been described, so a failed description damages nothing. The
        task is detached, so raising would have no caller to tell."""
        conn = await _connection(db_session)
        conn.description = "The previous description."
        conn.description_schema_hash = "stale-hash"
        await db_session.flush()

        async def exploding(*args: object, **kwargs: object) -> str:
            raise TextGenError("provider is down")

        monkeypatch.setattr(report_llm, "generate_text", exploding)

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == "The previous description."
        assert conn.description_schema_hash == "stale-hash"

    async def test_a_repository_with_no_usable_model_is_left_alone(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = await _connection(db_session, answerable=False)
        prompts = _replies(monkeypatch, "Should never be asked for.")

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == ""
        assert prompts == []

    async def test_an_empty_allow_list_is_left_alone(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connection created but not yet curated: there is nothing to
        describe, which is why nothing is dispatched at create time either."""
        conn = await _connection(db_session, schema=[])
        prompts = _replies(monkeypatch, "Should never be asked for.")

        await run_connection_description(conn.id, session_factory=_fixed_factory(db_session))

        await db_session.refresh(conn)
        assert conn.description == ""
        assert prompts == []


class TestTheCondition:
    """`description_is_current` is asked by both the dispatcher and the worker,
    so the two cannot disagree about what "needs describing" means."""

    async def test_absent_is_not_current(self, db_session: AsyncSession) -> None:
        conn = await _connection(db_session)
        assert description_is_current(conn) is False

    async def test_described_for_this_allow_list_is_current(self, db_session: AsyncSession) -> None:
        conn = await _connection(db_session)
        conn.description = "Customer order totals."
        conn.description_schema_hash = schema_fingerprint(SCHEMA)
        assert description_is_current(conn) is True

    async def test_described_for_an_older_allow_list_is_not(self, db_session: AsyncSession) -> None:
        conn = await _connection(db_session)
        conn.description = "Customer order totals."
        conn.description_schema_hash = schema_fingerprint(SCHEMA)
        conn.exposed_schema = WIDER
        assert description_is_current(conn) is False
