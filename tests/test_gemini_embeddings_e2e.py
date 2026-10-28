"""End-to-end test of the Gemini embedding path through the full HTTP stack.

Unlike the other API tests, this does **not** override ``get_embedder`` with a fake.
Instead it seeds a verified Gemini provider key and monkeypatches only the genai SDK
boundary (``embeddings.gemini._genai_client``), so the *real* ``GeminiEmbeddingProvider``
runs through the dependency, ingestion, and retrieval — proving the whole wiring offline:

    upload → background ingest (embed as RETRIEVAL_DOCUMENT) → query (embed as
    RETRIEVAL_QUERY) → access-filtered retrieval → cited answer.

The fake genai client returns one constant vector per input; since the real provider
L2-normalizes, every vector collapses to the same unit vector, so any uploaded chunk is
a perfect match for any question and retrieval always returns it.
"""

from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import contextvault.embeddings.gemini as gemini_mod
from contextvault.api.deps import get_ingestion_session_factory, get_llm_builder
from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.llm.base import Answer, Citation
from contextvault.main import create_app
from contextvault.models import Grant, LLMProviderName, ProviderSetting, Repository, Role, User
from contextvault.retrieval import RetrievedChunk
from contextvault.services import users as user_service

# --- fake genai SDK boundary (records the task types the real provider requests) ---


class _FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeResponse:
    def __init__(self, embeddings: list[_FakeEmbedding]) -> None:
        self.embeddings = embeddings


class _FakeModels:
    def __init__(self, recorder: dict[str, list[str]]) -> None:
        self._recorder = recorder

    def embed_content(self, *, model: str, contents: Sequence[str], config) -> _FakeResponse:  # type: ignore[no-untyped-def]
        self._recorder.setdefault("task_types", []).append(config.task_type)
        dim = config.output_dimensionality
        # One constant vector per input → identical after L2-normalization → perfect match.
        vector = [1.0] + [0.0] * (dim - 1)
        return _FakeResponse([_FakeEmbedding(vector) for _ in contents])


class _FakeClient:
    def __init__(self, recorder: dict[str, list[str]]) -> None:
        self.models = _FakeModels(recorder)


# --- fake LLM generation seam (so the query answers without a real provider call) ---


class _RecordingProvider:
    def __init__(self) -> None:
        self.received: list[RetrievedChunk] | None = None

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        self.received = list(chunks)
        first = chunks[0]
        return Answer(
            text="Grounded answer [1].",
            citations=[
                Citation(
                    number=1,
                    chunk_id=first.chunk_id,
                    source_id=first.source_id,
                    char_start=first.char_start,
                    char_end=first.char_end,
                )
            ],
            not_in_vault=False,
        )


async def _recording_builder(session: AsyncSession, repo: Repository) -> _RecordingProvider:
    return _RecordingProvider()


def _fixed_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


# --- helpers ---


async def _user(db_session: AsyncSession, role: Role, username: str) -> User:
    return await user_service.create_user(db_session, username=username, password="pw", role=role)


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_gemini_embeddings_end_to_end(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder: dict[str, list[str]] = {}
    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: _FakeClient(recorder))

    # A verified Gemini key: it satisfies the embedder dependency AND makes the repo
    # answerable (repo generates via Gemini too), so one key drives the whole loop.
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.GEMINI,
            api_key_encrypted=encrypt("gemini-test-key"),
            verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    repo = Repository(
        name="Vault", llm_provider=LLMProviderName.GEMINI, llm_model="gemini-2.5-flash"
    )
    db_session.add(repo)
    await db_session.flush()

    await _user(db_session, Role.ADMIN, "admin")
    reader = await _user(db_session, Role.USER, "reader")
    db_session.add(Grant(user_id=reader.id, repository_id=repo.id, expires_at=None))
    await db_session.flush()

    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    # get_embedder is intentionally NOT overridden — the real GeminiEmbeddingProvider runs.
    app.dependency_overrides[get_session] = _use_test_session
    app.dependency_overrides[get_ingestion_session_factory] = lambda: _fixed_factory(db_session)
    app.dependency_overrides[get_llm_builder] = lambda: _recording_builder

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        admin_token = await _token(client, "admin")
        reader_token = await _token(client, "reader")

        # Upload a document → ingestion (embed as document) runs inline via the overrides.
        upload = await client.post(
            f"/repositories/{repo.id}/sources",
            files={
                "file": ("policy.txt", b"The vault retention policy is thirty days.", "text/plain")
            },
            headers=_auth(admin_token),
        )
        assert upload.status_code == 201

        # Query → embed as query → retrieve → cited answer.
        query = await client.post(
            f"/repositories/{repo.id}/query",
            json={"question": "What is the retention policy?"},
            headers=_auth(reader_token),
        )

    assert query.status_code == 200, query.text
    body = query.json()
    assert body["citations"], "the uploaded chunk should have been retrieved and cited"

    # The real provider embedded the document as RETRIEVAL_DOCUMENT (ingest) and the
    # question as RETRIEVAL_QUERY (retrieval) — the whole task-typed path ran offline.
    assert "RETRIEVAL_DOCUMENT" in recorder["task_types"]
    assert "RETRIEVAL_QUERY" in recorder["task_types"]
