"""Admin Notes as sources — the curation flywheel.

An admin writes an answer (typically to a knowledge gap); it becomes an
``admin_note`` source, is ingested (chunk+embed) exactly like an upload, and is then
retrievable and **cited as a first-class, Verified source attributed to the admin**.

The end-to-end test drives the real stack offline (same fake embedder/provider
harness as ``test_query_api``): create a note → its background ingestion runs inline
→ a user query retrieves it → the response cites it with ``verified = true`` and the
admin's nickname. That is the whole loop: admin curation → permanently smarter vault.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_embedder, get_ingestion_session_factory, get_llm_builder
from contextvault.core.config import get_settings
from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.llm.base import Answer, Citation
from contextvault.llm.citations import not_in_vault_answer
from contextvault.main import create_app
from contextvault.models import Grant, LLMProviderName, ProviderSetting, Repository, Role, User
from contextvault.retrieval import RetrievedChunk
from contextvault.services import users as user_service


class FakeEmbedder:
    """Every text maps to the same vector, so any ingested chunk is retrievable."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        return [[0.1] * self._dimension for _ in texts]


class RecordingProvider:
    """Grounded cited answer on the first chunk; honest not-in-vault when none."""

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


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


async def test_admin_creates_note(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "curator")
    resp = await client.post(
        f"/repositories/{repo.id}/admin-notes",
        json={"title": "Refund policy", "content": "Refunds are issued within 30 days."},
        headers=_auth(await _token(client, "curator")),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "admin_note"
    assert body["title"] == "Refund policy"
    assert body["original_filename"] is None


async def test_create_note_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.USER, "regular")
    resp = await client.post(
        f"/repositories/{repo.id}/admin-notes",
        json={"title": "Nope", "content": "Not allowed."},
        headers=_auth(await _token(client, "regular")),
    )
    assert resp.status_code == 403


async def test_create_note_rejects_empty_content(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "curator")
    resp = await client.post(
        f"/repositories/{repo.id}/admin-notes",
        json={"title": "Empty", "content": ""},
        headers=_auth(await _token(client, "curator")),
    )
    assert resp.status_code == 422


async def test_create_note_unknown_repo_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "curator")
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/admin-notes",
        json={"title": "Ghost", "content": "No repo."},
        headers=_auth(await _token(client, "curator")),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# The flywheel: note → indexed → retrievable → cited Verified to the admin
# --------------------------------------------------------------------------- #


async def test_admin_note_is_retrievable_and_cited_verified(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "curator")
    admin_token = await _token(client, "curator")

    # The admin answers a gap by writing a note; its ingestion runs inline.
    note = await client.post(
        f"/repositories/{repo.id}/admin-notes",
        json={
            "title": "How do I reset the VPN?",
            "content": "Open the portal and click Reset VPN.",
        },
        headers=_auth(admin_token),
    )
    assert note.status_code == 201
    note_id = note.json()["id"]

    # A granted user asks — the note is now retrievable and grounds the answer.
    reader = await _user(db_session, Role.USER, "reader")
    await _grant(db_session, reader.id, repo.id)
    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "How do I reset the VPN?"},
        headers=_auth(await _token(client, "reader")),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["not_in_vault"] is False
    assert len(body["sources"]) == 1
    source = body["sources"][0]
    assert source["id"] == note_id
    assert source["kind"] == "admin_note"
    assert source["verified"] is True  # the Verified badge
    assert source["author"] == "curator"  # cited to the admin's nickname


async def test_admin_note_embeds_title_and_content(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A terse admin note (e.g. answering a knowledge gap) is titled with the
    question and only its content holds the answer. Ingestion must embed the title
    too, or a terse answer like "10 градусів" is ungroundable on its own. The stored
    passage (what gets chunked + embedded) must carry both the title and the content."""
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "curator")
    note = await client.post(
        f"/repositories/{repo.id}/admin-notes",
        json={"title": "яка погода в києві", "content": "10 градусів"},
        headers=_auth(await _token(client, "curator")),
    )
    assert note.status_code == 201
    note_id = note.json()["id"]

    # Read back the stored passage via the API (ingestion set it to the text it
    # chunked + embedded). A granted reader is required by the source-content route.
    reader = await _user(db_session, Role.USER, "reader")
    await _grant(db_session, reader.id, repo.id)
    got = await client.get(
        f"/repositories/{repo.id}/sources/{note_id}",
        headers=_auth(await _token(client, "reader")),
    )
    assert got.status_code == 200
    content = got.json()["content"]
    assert "яка погода в києві" in content  # the title (question) is embedded, not just the answer
    assert "10 градусів" in content


async def test_uploaded_document_is_not_verified(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Regression: an ordinary uploaded document is a source too, but it is neither
    Verified nor attributed to an author — only Admin Notes are."""
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "curator")
    admin_token = await _token(client, "curator")
    upload = await client.post(
        f"/repositories/{repo.id}/sources",
        files={"file": ("policy.txt", b"The office opens at nine.", "text/plain")},
        headers=_auth(admin_token),
    )
    assert upload.status_code == 201

    reader = await _user(db_session, Role.USER, "reader")
    await _grant(db_session, reader.id, repo.id)
    resp = await client.post(
        f"/repositories/{repo.id}/query",
        json={"question": "When does the office open?"},
        headers=_auth(await _token(client, "reader")),
    )
    assert resp.status_code == 200
    source = resp.json()["sources"][0]
    assert source["kind"] == "document"
    assert source["verified"] is False
    assert source["author"] is None
