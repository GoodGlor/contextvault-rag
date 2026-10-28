"""DB-backed tests for the retrieval service.

The service sits above the raw access-filtered search: it embeds the
question, runs the search scoped to the user's granted repo, and applies a
relevance threshold so the *weak/empty* case is detectable — that signal feeds
the honest "not in this vault" answer and the knowledge-gap dashboard.
These tests drive the embed→search→threshold path with a fake
embedder that maps known questions to known vectors, so similarity is exact.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.models import (
    Chunk,
    Grant,
    Repository,
    Role,
    Source,
    SourceKind,
    SourceStatus,
)
from contextvault.retrieval import RetrievalResult, retrieve
from contextvault.services import users as user_service


def _embedding(a: float, b: float) -> list[float]:
    """A vector living in the first two dims, padded to the model width."""
    dim = get_settings().embedding_dim
    return [a, b, *([0.0] * (dim - 2))]


class _FakeEmbedder:
    """Maps each question string to a caller-chosen vector — deterministic sim."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    @property
    def dimension(self) -> int:
        return get_settings().embedding_dim

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        return [self._mapping[t] for t in texts]


async def _user(session: AsyncSession, name: str) -> uuid.UUID:
    user = await user_service.create_user(session, username=name, password="pw", role=Role.USER)
    return user.id


async def _repo(session: AsyncSession, name: str = "Vault") -> Repository:
    repo = Repository(name=name)
    session.add(repo)
    await session.flush()
    return repo


async def _source(session: AsyncSession, repo: Repository) -> Source:
    # DONE: retrieval only reads chunks of sources that finished ingesting.
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.DOCUMENT,
        title="doc.txt",
        status=SourceStatus.DONE,
    )
    session.add(source)
    await session.flush()
    return source


async def _chunk(
    session: AsyncSession,
    source: Source,
    *,
    ordinal: int,
    content: str,
    embedding: Sequence[float] | None,
    char_start: int | None = None,
    char_end: int | None = None,
) -> Chunk:
    chunk = Chunk(
        source_id=source.id,
        repository_id=source.repository_id,
        ordinal=ordinal,
        content=content,
        char_start=char_start,
        char_end=char_end,
        embedding=list(embedding) if embedding is not None else None,
    )
    session.add(chunk)
    await session.flush()
    return chunk


async def _grant(
    session: AsyncSession,
    user_id: uuid.UUID,
    repo: Repository,
    *,
    expires_at: datetime | None = None,
) -> None:
    session.add(Grant(user_id=user_id, repository_id=repo.id, expires_at=expires_at))
    await session.flush()


async def test_embeds_question_and_returns_ranked_relevant_chunks(
    db_session: AsyncSession,
) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "reader")
    await _grant(db_session, user_id, repo)
    source = await _source(db_session, repo)
    await _chunk(
        db_session,
        source,
        ordinal=0,
        content="near",
        embedding=_embedding(1.0, 0.0),
        char_start=0,
        char_end=4,
    )
    await _chunk(db_session, source, ordinal=1, content="mid", embedding=_embedding(1.0, 1.0))

    embedder = _FakeEmbedder({"what?": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session,
        question="what?",
        repository_id=repo.id,
        user_id=user_id,
        embedder=embedder,
    )

    assert isinstance(result, RetrievalResult)
    assert [c.content for c in result.chunks] == ["near", "mid"]  # ranked, closest first
    assert result.has_results is True
    assert result.chunks[0].char_start == 0 and result.chunks[0].char_end == 4  # offsets carried
    assert result.top_score == 1.0


async def test_ungranted_user_gets_no_results(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "stranger")  # no grant
    source = await _source(db_session, repo)
    await _chunk(db_session, source, ordinal=0, content="secret", embedding=_embedding(1.0, 0.0))

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session, question="q", repository_id=repo.id, user_id=user_id, embedder=embedder
    )

    assert result.chunks == []
    assert result.has_results is False
    assert result.top_score is None  # nothing retrievable at all → no signal


async def test_weak_results_below_threshold_are_detectable(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "reader")
    await _grant(db_session, user_id, repo)
    source = await _source(db_session, repo)
    # Chunk is orthogonal to the question → cosine similarity 0.0, below threshold.
    await _chunk(db_session, source, ordinal=0, content="unrelated", embedding=_embedding(0.0, 1.0))

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session,
        question="q",
        repository_id=repo.id,
        user_id=user_id,
        embedder=embedder,
        min_score=0.5,
    )

    assert result.chunks == []  # nothing sufficiently relevant → "not in this vault"
    assert result.has_results is False
    assert result.top_score == 0.0  # but a chunk WAS found — distinguishes gap from no-data


async def test_min_score_threshold_is_configurable(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "reader")
    await _grant(db_session, user_id, repo)
    source = await _source(db_session, repo)
    # 45° from the query → cosine similarity ~0.707.
    await _chunk(db_session, source, ordinal=0, content="mid", embedding=_embedding(1.0, 1.0))

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})

    strict = await retrieve(
        db_session,
        question="q",
        repository_id=repo.id,
        user_id=user_id,
        embedder=embedder,
        min_score=0.9,
    )
    assert strict.chunks == []  # 0.707 < 0.9

    lax = await retrieve(
        db_session,
        question="q",
        repository_id=repo.id,
        user_id=user_id,
        embedder=embedder,
        min_score=0.5,
    )
    assert [c.content for c in lax.chunks] == ["mid"]  # 0.707 >= 0.5


async def test_min_score_defaults_to_setting(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "reader")
    await _grant(db_session, user_id, repo)
    source = await _source(db_session, repo)
    await _chunk(db_session, source, ordinal=0, content="exact", embedding=_embedding(1.0, 0.0))

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session, question="q", repository_id=repo.id, user_id=user_id, embedder=embedder
    )
    # An exact match (similarity 1.0) clears any sane default threshold.
    assert [c.content for c in result.chunks] == ["exact"]


async def test_granted_repo_with_no_chunks_has_no_signal(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "reader")
    await _grant(db_session, user_id, repo)  # granted, but the vault is empty

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session, question="q", repository_id=repo.id, user_id=user_id, embedder=embedder
    )

    assert result.chunks == []
    assert result.has_results is False
    assert result.top_score is None


async def test_expired_grant_blocks_retrieval(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "lapsed")
    await _grant(db_session, user_id, repo, expires_at=datetime.now(UTC) - timedelta(days=1))
    source = await _source(db_session, repo)
    await _chunk(db_session, source, ordinal=0, content="secret", embedding=_embedding(1.0, 0.0))

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session, question="q", repository_id=repo.id, user_id=user_id, embedder=embedder
    )

    assert result.chunks == []
    assert result.top_score is None


async def test_k_limits_result_count(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    user_id = await _user(db_session, "reader")
    await _grant(db_session, user_id, repo)
    source = await _source(db_session, repo)
    for i in range(5):
        await _chunk(db_session, source, ordinal=i, content=f"c{i}", embedding=_embedding(1.0, 0.0))

    embedder = _FakeEmbedder({"q": _embedding(1.0, 0.0)})
    result = await retrieve(
        db_session, question="q", repository_id=repo.id, user_id=user_id, embedder=embedder, k=3
    )
    assert len(result.chunks) == 3
