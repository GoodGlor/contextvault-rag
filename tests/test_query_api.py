"""Integration tests for the query endpoint — the full RAG loop.

`POST /repositories/{id}/query` ties the pieces together: auth + active-grant
check → access-filtered retrieval → provider generation → cited response. Driven
with httpx.AsyncClient over the async ``db_session`` fixture (see
test_sources_api). The embedder and the LLM provider are overridden so the loop
runs offline: the FakeEmbedder makes every vector identical (so an uploaded doc
is always retrievable), and the RecordingProvider records the chunks it was
handed and returns a grounded, cited answer — proving retrieve→generate wiring
without any network call.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_embedder, get_ingestion_session_factory, get_llm_builder
from contextvault.core.config import get_settings
from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.llm.base import Answer, Citation
from contextvault.llm.citations import NOT_IN_VAULT, not_in_vault_answer
from contextvault.main import create_app
from contextvault.models import (
    Chunk,
    ConversationTurn,
    Grant,
    LLMProviderName,
    ProviderSetting,
    Repository,
    Role,
    Source,
    SourceStatus,
    User,
)
from contextvault.retrieval import RetrievedChunk
from contextvault.services import users as user_service


class FakeEmbedder:
    """Deterministic embedder of the configured width, so vectors fit pgvector.

    Every text maps to the same vector, so any uploaded chunk is a perfect match
    for any question — retrieval always returns the repo's chunks.
    """

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        return [[0.1] * self._dimension for _ in texts]


class RecordingProvider:
    """Fake ``LLMProvider``: records the chunks it got, returns a cited answer.

    Mirrors the real providers' honest short-circuit — no chunks yields the
    ``not_in_vault`` answer — so the endpoint's empty-retrieval path is exercised
    exactly as production behaves.
    """

    def __init__(self) -> None:
        self.received: list[RetrievedChunk] | None = None
        self.received_history: list[tuple[str, str]] | None = None

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        self.received = list(chunks)
        self.received_history = list(history)
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


class RecordingBuilder:
    """Fake per-repo provider builder: records the repo it was asked to route for
    and returns the fake provider. Standing in for ``build_repo_llm``, it proves the
    endpoint hands the *repository's own* stored config to the builder
    instead of ignoring it for a process-wide default, as the pre-#25 seam did.
    """

    def __init__(self, provider: RecordingProvider) -> None:
        self._provider = provider
        self.built_for: list[Repository] = []

    async def __call__(self, session: AsyncSession, repo: Repository) -> RecordingProvider:
        self.built_for.append(repo)
        return self._provider


def _fixed_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider()


@pytest.fixture
def builder(provider: RecordingProvider) -> RecordingBuilder:
    return RecordingBuilder(provider)


@pytest.fixture
async def client(
    db_session: AsyncSession, builder: RecordingBuilder
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder(get_settings().embedding_dim)
    app.dependency_overrides[get_ingestion_session_factory] = lambda: _fixed_factory(db_session)
    app.dependency_overrides[get_llm_builder] = lambda: builder

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _user(db_session: AsyncSession, role: Role, username: str) -> User:
    return await user_service.create_user(db_session, username=username, password="pw", role=role)


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


async def _repo(db_session: AsyncSession, *, configured: bool = True) -> Repository:
    repo = Repository(name="Vault")
    # A repository must be answerable before it can generate: a model picked whose
    # provider has a verified key. Queries below drive the generation
    # path, so make it answerable by default — seed the provider key too.
    if configured:
        db_session.add(
            ProviderSetting(
                provider=LLMProviderName.OPENAI,
                api_key_encrypted=encrypt("sk-test-key"),
                verified_at=datetime.now(UTC),
            )
        )
        repo.llm_provider = LLMProviderName.OPENAI
        repo.llm_model = "gpt-4o"
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _grant(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    repository_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
) -> None:
    db_session.add(Grant(user_id=user_id, repository_id=repository_id, expires_at=expires_at))
    await db_session.flush()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _upload(
    client: AsyncClient,
    db_session: AsyncSession,
    repo_id: uuid.UUID,
    admin_token: str,
    body: bytes,
) -> None:
    """Upload a document as admin, running ingestion inline (via the overrides).

    Asserts the document is actually *retrievable* afterwards, not merely that the
    upload was accepted. A 201 only says the row was created — ingestion runs as a
    background task and records its failures on the source rather than raising, so
    an ingestion that produced nothing used to surface much later as a wrong
    *answer*, pointing at the LLM path instead of at ingestion. These assertions
    make the layer that broke name itself.
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


async def test_query_requires_authentication(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    resp = await client.post(f"/repositories/{repo.id}/query", json={"question": "anything?"})
    assert resp.status_code == 401


async def test_query_unknown_repository_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "u404")
    token = await _token(client, "u404")
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/query",
        json={"question": "anything?"},
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_query_forbidden_without_grant(db_session: AsyncSession, client: AsyncClient) -> None:
    # Access is enforced: a real repo the user was never granted is denied.
    repo = await _repo(db_session)
    await _user(db_session, Role.USER, "ungranted")
    token = await _token(client, "ungranted")
    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "anything?"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_query_forbidden_when_grant_expired(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "expired")
    await _grant(
        db_session,
        user.id,
        repo.id,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    token = await _token(client, "expired")
    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "anything?"},
        headers=_auth(token),
    )
    assert resp.status_code == 403


async def test_query_rejects_empty_question(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "blankq")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "blankq")
    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": ""},
        headers=_auth(token),
    )
    assert resp.status_code == 422


async def test_query_returns_grounded_cited_answer(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin1")
    admin_token = await _token(client, "admin1")
    await _upload(
        client, db_session, repo.id, admin_token, b"The vault stores curated policy documents."
    )

    user = await _user(db_session, Role.USER, "reader")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "reader")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What does the vault store?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["answer"] == "Grounded answer [1]."
    assert body["not_in_vault"] is False

    # One citation, resolving [1] to a real retrieved chunk + its source.
    assert len(body["citations"]) == 1
    citation = body["citations"][0]
    assert citation["number"] == 1
    assert citation["chunk_id"]
    assert citation["source_id"]

    # Source references list the cited document so the UI can link to it.
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert source["id"] == citation["source_id"]
    assert source["title"] == "policy.txt"
    assert source["original_filename"] == "policy.txt"

    # The provider was grounded on the retrieved chunks, not the raw question.
    assert provider.received is not None
    assert len(provider.received) >= 1


async def test_query_threads_conversation_history_to_the_provider(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    # A follow-up question is threaded with the prior turn's history — but that
    # history is server-authoritative: it comes from the DB-saved conversation
    # (populated by the first query), never from the request body.
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin2")
    admin_token = await _token(client, "admin2")
    await _upload(client, db_session, repo.id, admin_token, b"Part-timers accrue pro-rated leave.")

    user = await _user(db_session, Role.USER, "reader2")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "reader2")

    first = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What is the PTO policy?"},
        headers=_auth(token),
    )
    assert first.status_code == 200
    first_answer = first.json()["answer"]

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "and for part-timers?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert provider.received_history == [("What is the PTO policy?", first_answer)]


async def test_query_history_field_in_request_body_is_ignored(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    # `history` is no longer part of the request schema; a client that still sends
    # one gets neither a validation error nor any effect — the server-loaded
    # (empty, for a first turn) history is what the provider actually receives.
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "reader2b")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "reader2b")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={
            "question": "What does the vault store?",
            "history": [{"question": "spoofed?", "answer": "spoofed!"}],
        },
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert provider.received_history == []


async def test_query_persists_a_turn(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin2c")
    admin_token = await _token(client, "admin2c")
    await _upload(
        client, db_session, repo.id, admin_token, b"The vault stores curated policy documents."
    )

    user = await _user(db_session, Role.USER, "reader2c")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "reader2c")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What is the VPN?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()

    turns = (await db_session.execute(sa.select(ConversationTurn))).scalars().all()
    assert len(turns) == 1
    turn = turns[0]
    assert turn.question == "What is the VPN?"
    assert turn.answer == body["answer"]
    assert turn.not_in_vault == body["not_in_vault"]
    assert turn.citations == body["citations"]
    assert turn.sources == body["sources"]


async def test_second_query_uses_db_history_not_client(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    # The first question creates a turn; the second must receive that turn as
    # history even though neither request body carries a `history` field.
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin2d")
    admin_token = await _token(client, "admin2d")
    await _upload(
        client, db_session, repo.id, admin_token, b"The vault stores curated policy documents."
    )

    user = await _user(db_session, Role.USER, "reader2d")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "reader2d")

    first = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "First?"},
        headers=_auth(token),
    )
    assert first.status_code == 200
    first_answer = first.json()["answer"]

    second = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "Second?"},
        headers=_auth(token),
    )
    assert second.status_code == 200
    assert provider.received_history == [("First?", first_answer)]

    turns = (
        (await db_session.execute(sa.select(ConversationTurn).order_by(ConversationTurn.ordinal)))
        .scalars()
        .all()
    )
    assert len(turns) == 2
    assert turns[0].question == "First?"
    assert turns[1].question == "Second?"


async def test_query_without_history_passes_the_provider_an_empty_conversation(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin3")
    admin_token = await _token(client, "admin3")
    await _upload(
        client, db_session, repo.id, admin_token, b"The vault stores curated policy documents."
    )

    user = await _user(db_session, Role.USER, "reader3")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "reader3")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What does the vault store?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert provider.received_history == []


async def test_query_not_in_vault_when_no_relevant_chunks(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    # Granted, but the repo has no sources — retrieval is empty, so the honest
    # "not in this vault" answer comes back flagged, with no citations/sources.
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "emptyvault")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "emptyvault")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What is not here?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["not_in_vault"] is True
    assert body["answer"] == NOT_IN_VAULT
    assert body["citations"] == []
    assert body["sources"] == []
    assert provider.received == []


async def test_not_in_vault_turn_persists_and_replays_as_history(
    db_session: AsyncSession, client: AsyncClient, provider: RecordingProvider
) -> None:
    # A refusal is still a turn: it must be persisted (not_in_vault=True) and,
    # on the next question, replayed to the provider as prior history — the
    # admin's honest "not in this vault" is part of the conversation too.
    repo = await _repo(db_session)
    user = await _user(db_session, Role.USER, "emptyvault2")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "emptyvault2")

    first = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "What is not here?"},
        headers=_auth(token),
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["not_in_vault"] is True
    first_answer = first_body["answer"]

    turns = (
        (await db_session.execute(sa.select(ConversationTurn).order_by(ConversationTurn.ordinal)))
        .scalars()
        .all()
    )
    assert len(turns) == 1
    assert turns[0].question == "What is not here?"
    assert turns[0].answer == first_answer
    assert turns[0].not_in_vault is True

    second = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "Anything else?"},
        headers=_auth(token),
    )
    assert second.status_code == 200
    assert provider.received_history == [("What is not here?", first_answer)]


async def test_query_rejects_unconfigured_repository(
    db_session: AsyncSession,
    client: AsyncClient,
    provider: RecordingProvider,
    builder: RecordingBuilder,
) -> None:
    # A granted user querying a repo with no LLM configured gets a clear error,
    # not an answer — every repo must be configured before use.
    repo = await _repo(db_session, configured=False)
    user = await _user(db_session, Role.USER, "noconfig")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "noconfig")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "anything?"},
        headers=_auth(token),
    )
    assert resp.status_code == 409
    assert "model" in resp.json()["detail"].lower()
    # Generation was never reached — the gate stops before a provider is even built.
    assert builder.built_for == []
    assert provider.received is None


async def test_query_routes_to_repository_configured_provider(
    db_session: AsyncSession, client: AsyncClient, builder: RecordingBuilder
) -> None:
    # The endpoint builds its provider from THIS repository's chosen provider/model —
    # rather than a process-wide default. Retrieval is
    # empty here, but the provider is built before generation, so the routing decision
    # is still observable. The key itself is shared from the global provider settings.
    repo = await _repo(db_session)  # openai / gpt-4o, provider key seeded
    user = await _user(db_session, Role.USER, "router")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "router")

    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "anything?"},
        headers=_auth(token),
    )
    assert resp.status_code == 200

    assert len(builder.built_for) == 1
    routed = builder.built_for[0]
    assert routed.id == repo.id
    assert routed.llm_provider == LLMProviderName.OPENAI
    assert routed.llm_model == "gpt-4o"
