"""Query-logging tests.

Every answered query writes one ``QueryLog`` row — the raw material for the
knowledge-gap dashboard (#31) and analytics (#33). These tests drive the real query
endpoint (offline, via the same fake embedder/provider harness as
``test_query_api``) and assert the logged fields:

* who asked (``user_id``), against which repo, the question text;
* the retrieval signal — ``top_score`` and ``chunk_count``;
* whether the answer was grounded (``not_in_vault``).

A final test checks that deleting the asker anonymizes
their logged questions (``user_id`` → NULL) rather than deleting them, because the
FK is ``ON DELETE SET NULL``.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_embedder, get_ingestion_session_factory, get_llm_builder
from contextvault.core.config import get_settings
from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.llm.base import Answer, Citation
from contextvault.llm.citations import not_in_vault_answer
from contextvault.main import create_app
from contextvault.models import (
    Chunk,
    Grant,
    LLMProviderName,
    ProviderSetting,
    QueryLog,
    Repository,
    Role,
    Source,
    SourceStatus,
    User,
)
from contextvault.retrieval import RetrievedChunk
from contextvault.services import users as user_service


class FakeEmbedder:
    """Deterministic embedder: every text maps to the same vector, so any chunk is
    always retrievable for any question."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        return [[0.1] * self._dimension for _ in texts]


class RecordingProvider:
    """Fake ``LLMProvider``: grounded cited answer when given chunks, the honest
    ``not_in_vault`` answer when given none."""

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        if not chunks:
            return not_in_vault_answer()
        first = chunks[0]
        citation = Citation(
            number=1,
            chunk_id=first.chunk_id,
            source_id=first.source_id,
            char_start=first.char_start,
            char_end=first.char_end,
        )
        return Answer(text="Grounded answer [1].", citations=[citation], not_in_vault=False)


def _fixed_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder(get_settings().embedding_dim)
    app.dependency_overrides[get_ingestion_session_factory] = lambda: _fixed_factory(db_session)
    app.dependency_overrides[get_llm_builder] = lambda: _fake_builder

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _user(db_session: AsyncSession, role: Role, username: str) -> User:
    return await user_service.create_user(db_session, username=username, password="pw", role=role)


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


async def _fake_builder(session: AsyncSession, repo: Repository) -> RecordingProvider:
    return RecordingProvider()


async def _repo(db_session: AsyncSession) -> Repository:
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.OPENAI,
            api_key_encrypted=encrypt("sk-test-key"),
            verified_at=datetime.now(UTC),
        )
    )
    repo = Repository(name="Vault", llm_provider=LLMProviderName.OPENAI, llm_model="gpt-4o")
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _grant(db_session: AsyncSession, user_id: uuid.UUID, repo_id: uuid.UUID) -> None:
    db_session.add(Grant(user_id=user_id, repository_id=repo_id))
    await db_session.flush()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _logs(db_session: AsyncSession) -> list[QueryLog]:
    result = await db_session.execute(sa.select(QueryLog).order_by(QueryLog.created_at))
    return list(result.scalars().all())


async def _upload(
    client: AsyncClient,
    db_session: AsyncSession,
    repo_id: uuid.UUID,
    admin_token: str,
    body: bytes,
) -> None:
    """Upload a document as admin, and prove it ended up retrievable.

    A 201 only says the source row was created; ingestion runs as a background
    task and records failures on the source rather than raising. Asserting the
    outcome here keeps a broken ingestion from masquerading as a logging defect.
    """
    resp = await client.post(
        f"/repositories/{repo_id}/sources",
        files={"file": ("policy.txt", body, "text/plain")},
        headers=_auth(admin_token),
    )
    assert resp.status_code == 201
    source_id = uuid.UUID(resp.json()["id"])

    source = await db_session.get(Source, source_id)
    assert source is not None
    assert source.status is SourceStatus.DONE, f"ingestion did not finish: {source.ingest_error}"

    embedded = (
        await db_session.execute(
            sa.select(sa.func.count())
            .select_from(Chunk)
            .where(Chunk.source_id == source_id, Chunk.embedding.is_not(None))
        )
    ).scalar_one()
    assert embedded >= 1, "ingestion stored no embedded chunks, so nothing is retrievable"


# --------------------------------------------------------------------------- #
# A grounded query is logged with its full signal
# --------------------------------------------------------------------------- #


async def test_grounded_query_is_logged(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin")
    admin_token = await _token(client, "admin")
    await _upload(
        client, db_session, repo.id, admin_token, b"The vault stores curated policy documents."
    )

    reader = await _user(db_session, Role.USER, "reader")
    await _grant(db_session, reader.id, repo.id)
    token = await _token(client, "reader")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What does the vault store?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    logs = await _logs(db_session)
    assert len(logs) == 1
    log = logs[0]
    assert log.user_id == reader.id
    assert log.repository_id == repo.id
    assert log.question == "What does the vault store?"
    # Ordered most diagnostic first: ``top_score`` is set whenever anything was
    # retrievable at all (it is recorded before the relevance threshold is
    # applied), while ``chunk_count`` counts only what cleared that threshold. A
    # retrieval failure therefore says which half broke, instead of surfacing as
    # the blanket "not in vault" the answer path reports.
    assert log.top_score is not None
    assert log.chunk_count >= 1
    assert log.not_in_vault is False


# --------------------------------------------------------------------------- #
# A "not in vault" query is logged as a knowledge gap
# --------------------------------------------------------------------------- #


async def test_not_in_vault_query_is_logged_as_gap(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    # Granted but the repo has no sources: retrieval is empty, the answer is the
    # honest "not in vault", and the log records the gap (chunk_count 0).
    repo = await _repo(db_session)
    reader = await _user(db_session, Role.USER, "reader")
    await _grant(db_session, reader.id, repo.id)
    token = await _token(client, "reader")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "Something not covered?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    logs = await _logs(db_session)
    assert len(logs) == 1
    log = logs[0]
    assert log.not_in_vault is True
    assert log.chunk_count == 0
    assert log.question == "Something not covered?"


# --------------------------------------------------------------------------- #
# Failed pre-generation gates do not produce a log
# --------------------------------------------------------------------------- #


async def test_denied_query_is_not_logged(db_session: AsyncSession, client: AsyncClient) -> None:
    # A user without a grant is refused before retrieval/generation — there is no
    # answered query, so nothing is logged.
    repo = await _repo(db_session)
    await _user(db_session, Role.USER, "ungranted")
    token = await _token(client, "ungranted")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "anything?"},
        headers=_auth(token),
    )
    assert resp.status_code == 403
    assert await _logs(db_session) == []


# --------------------------------------------------------------------------- #
# Deleting the asker anonymizes their logs
# --------------------------------------------------------------------------- #


async def test_deleting_user_anonymizes_their_query_logs(db_session: AsyncSession) -> None:
    reader = await _user(db_session, Role.USER, "reader")
    repo = Repository(name="Vault")
    db_session.add(repo)
    await db_session.flush()
    db_session.add(
        QueryLog(
            user_id=reader.id,
            repository_id=repo.id,
            question="Who asked this?",
            top_score=0.9,
            chunk_count=3,
            not_in_vault=False,
        )
    )
    await db_session.flush()

    await user_service.delete_user(db_session, reader)
    await db_session.flush()

    logs = await _logs(db_session)
    assert len(logs) == 1  # the question survives...
    assert logs[0].user_id is None  # ...anonymized, not deleted
    assert logs[0].question == "Who asked this?"
