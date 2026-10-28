"""End-to-end tests for the ingestion pipeline (parse→chunk→embed→store).

DB-backed: they use the ``db_session`` fixture and skip when no migrated
database is reachable (see conftest). The embedder is faked — a deterministic
vector per text of the configured width — so the suite stays fast and offline.
"""

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.core.crypto import encrypt
from contextvault.models import (
    Chunk,
    LLMProviderName,
    ProviderSetting,
    Repository,
    Source,
    SourceKind,
    SourceStatus,
)
from contextvault.services.ingestion import (
    EMBED_PROGRESS_SLICE,
    ingest_source,
    run_ingestion,
    run_web_ingestion,
)


class FakeEmbedder:
    """Deterministic ``EmbeddingProvider``: one fixed-width vector per text."""

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension
        self.calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * self._dimension for _ in texts]


class BoomEmbedder:
    """An embedder that always fails, to exercise the failure path."""

    @property
    def dimension(self) -> int:
        return get_settings().embedding_dim

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        raise RuntimeError("embed exploded")


def _fixed_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    """A session factory that always yields ``session`` without closing it."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


async def _make_source(session: AsyncSession) -> tuple[Repository, Source]:
    repo = Repository(name="Vault")
    session.add(repo)
    await session.flush()
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.DOCUMENT,
        title="Doc",
        original_filename="doc.txt",
    )
    session.add(source)
    await session.flush()
    return repo, source


async def _chunks_for(session: AsyncSession, source: Source) -> list[Chunk]:
    result = await session.execute(
        sa.select(Chunk).where(Chunk.source_id == source.id).order_by(Chunk.ordinal)
    )
    return list(result.scalars().all())


async def test_source_defaults_to_pending(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    refreshed = await db_session.get(Source, source.id)
    assert refreshed is not None
    assert refreshed.status is SourceStatus.PENDING


async def test_ingest_populates_chunks_and_marks_done(db_session: AsyncSession) -> None:
    repo, source = await _make_source(db_session)
    text = "Sentence number one. " * 200  # long enough for several chunks
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(
        db_session, source, filename="doc.txt", data=text.encode(), embedder=embedder
    )

    assert source.status is SourceStatus.DONE
    assert source.ingest_error is None
    assert source.content == text
    assert embedder.calls  # the embedder was actually used

    chunks = await _chunks_for(db_session, source)
    assert len(chunks) >= 2
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.repository_id == repo.id
        assert chunk.embedding is not None
        assert len(list(chunk.embedding)) == embedder.dimension
        # Offsets slice the chunk text back out of the source — citation-ready.
        assert text[chunk.char_start : chunk.char_end] == chunk.content


class ProgressSpyEmbedder(FakeEmbedder):
    """Records the progress the source reports at each embed call.

    Ingestion embeds a long document in slices and commits progress between them,
    so the value seen here climbs across calls rather than jumping at the end.
    """

    def __init__(self, dimension: int, source: Source) -> None:
        super().__init__(dimension)
        self._source = source
        self.seen_done: list[int | None] = []

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        self.seen_done.append(self._source.chunks_done)
        return super().embed(texts, task=task)


# Long enough to span several embed slices: at chunk_size 3000 / overlap 300 the
# window advances 2700 chars, so this yields ~220 chunks — three slices of 100.
_LONG_TEXT = "Речення номер один. " * 30000


async def test_ingest_records_final_chunk_progress(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    text = "Sentence number one. " * 200
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(
        db_session, source, filename="doc.txt", data=text.encode(), embedder=embedder
    )

    chunks = await _chunks_for(db_session, source)
    assert source.chunks_total == len(chunks)
    assert source.chunks_done == len(chunks)


async def test_progress_advances_while_embedding(db_session: AsyncSession) -> None:
    """A long document reports partial progress, not 0 then 100."""
    _, source = await _make_source(db_session)
    embedder = ProgressSpyEmbedder(get_settings().embedding_dim, source)

    await ingest_source(
        db_session, source, filename="doc.txt", data=_LONG_TEXT.encode(), embedder=embedder
    )

    assert len(embedder.calls) > 1  # embedded in slices, not one shot
    # Progress is known before any embedding starts, and climbs as slices land.
    assert all(seen is not None for seen in embedder.seen_done)
    progress = [seen for seen in embedder.seen_done if seen is not None]
    assert progress[0] == 0
    assert progress == sorted(progress)
    assert progress[-1] > 0
    assert source.chunks_done == source.chunks_total


async def test_total_is_known_before_embedding_starts(db_session: AsyncSession) -> None:
    """The bar needs a denominator from the first poll, not only at the end."""
    _, source = await _make_source(db_session)
    seen_total: list[int | None] = []

    class _TotalSpy(FakeEmbedder):
        def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
            seen_total.append(source.chunks_total)
            return super().embed(texts, task=task)

    await ingest_source(
        db_session,
        source,
        filename="doc.txt",
        data=_LONG_TEXT.encode(),
        embedder=_TotalSpy(get_settings().embedding_dim),
    )
    assert seen_total[0] is not None
    assert seen_total[0] == source.chunks_total


async def test_failed_ingest_keeps_progress_reached(db_session: AsyncSession) -> None:
    """Where it got to is diagnostic — a quota failure at 1900/4042 says a lot."""
    _, source = await _make_source(db_session)

    await ingest_source(
        db_session,
        source,
        filename="doc.txt",
        data=_LONG_TEXT.encode(),
        embedder=BoomEmbedder(),
    )

    assert source.status is SourceStatus.FAILED
    assert source.chunks_total is not None and source.chunks_total > 0


async def test_reingest_resets_progress(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)
    long_data = _LONG_TEXT.encode()
    await ingest_source(db_session, source, filename="doc.txt", data=long_data, embedder=embedder)
    first_total = source.chunks_total

    short = ("Short doc. " * 50).encode()
    await ingest_source(db_session, source, filename="doc.txt", data=short, embedder=embedder)

    assert first_total is not None and source.chunks_total is not None
    assert source.chunks_total < first_total
    assert source.chunks_done == source.chunks_total


class MidwayFailingEmbedder(FakeEmbedder):
    """Embeds ``ok_calls`` slices, then fails — a long document dying part-way."""

    def __init__(self, dimension: int, ok_calls: int) -> None:
        super().__init__(dimension)
        self._ok_calls = ok_calls

    def embed(self, texts: Sequence[str], *, task: str = "document") -> list[list[float]]:
        if len(self.calls) >= self._ok_calls:
            raise RuntimeError("embed exploded mid-document")
        return super().embed(texts, task=task)


async def test_failed_ingest_keeps_embedded_chunks_for_resume(db_session: AsyncSession) -> None:
    """Work already paid for to the embedding API must survive the failure.

    Under a hard daily quota, discarding embedded chunks means every retry re-spends
    the whole document — which is how a day's allowance disappears. Retrieval
    excludes non-DONE sources, so keeping them is safe.
    """
    _, source = await _make_source(db_session)
    embedder = MidwayFailingEmbedder(get_settings().embedding_dim, ok_calls=1)

    await ingest_source(
        db_session, source, filename="doc.txt", data=_LONG_TEXT.encode(), embedder=embedder
    )

    assert source.status is SourceStatus.FAILED
    assert len(embedder.calls) == 1
    kept = await _chunks_for(db_session, source)
    assert len(kept) == EMBED_PROGRESS_SLICE  # the one slice that succeeded
    assert source.chunks_done == EMBED_PROGRESS_SLICE


async def test_reingest_resumes_and_reembeds_only_the_tail(db_session: AsyncSession) -> None:
    """The whole point: a retry pays only for what is missing."""
    _, source = await _make_source(db_session)
    data = _LONG_TEXT.encode()
    await ingest_source(
        db_session,
        source,
        filename="doc.txt",
        data=data,
        embedder=MidwayFailingEmbedder(get_settings().embedding_dim, ok_calls=2),
    )
    # Bound to a local so mypy does not narrow ``source.status`` for the rest of the
    # test — it changes to DONE once the resumed run finishes.
    after_failure = source.status
    assert after_failure is SourceStatus.FAILED
    already = len(await _chunks_for(db_session, source))
    assert already == 2 * EMBED_PROGRESS_SLICE

    resumed = FakeEmbedder(get_settings().embedding_dim)
    await ingest_source(db_session, source, filename="doc.txt", data=data, embedder=resumed)

    assert source.status is SourceStatus.DONE
    total = source.chunks_total
    assert total is not None
    # Not one text already embedded was sent again.
    reembedded = sum(len(call) for call in resumed.calls)
    assert reembedded == total - already
    chunks = await _chunks_for(db_session, source)
    assert [c.ordinal for c in chunks] == list(range(total))


async def test_reingest_of_changed_document_does_not_reuse_stale_chunks(
    db_session: AsyncSession,
) -> None:
    """Resume is only safe where the new chunking still matches the stored text."""
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)
    await ingest_source(
        db_session, source, filename="doc.txt", data=_LONG_TEXT.encode(), embedder=embedder
    )

    changed = ("Зовсім інший текст. " * 30000).encode()
    fresh = FakeEmbedder(get_settings().embedding_dim)
    await ingest_source(db_session, source, filename="doc.txt", data=changed, embedder=fresh)

    assert source.status is SourceStatus.DONE
    chunks = await _chunks_for(db_session, source)
    assert all("Зовсім інший" in c.content for c in chunks)
    # Content differs from the first chunk onward, so nothing was reusable.
    assert sum(len(call) for call in fresh.calls) == source.chunks_total


async def test_reingest_of_identical_document_costs_nothing(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    data = _LONG_TEXT.encode()
    await ingest_source(
        db_session,
        source,
        filename="doc.txt",
        data=data,
        embedder=FakeEmbedder(get_settings().embedding_dim),
    )

    again = FakeEmbedder(get_settings().embedding_dim)
    await ingest_source(db_session, source, filename="doc.txt", data=data, embedder=again)

    assert source.status is SourceStatus.DONE
    assert again.calls == []  # every chunk was already embedded


async def test_long_document_stores_every_chunk_in_order(db_session: AsyncSession) -> None:
    """Slice-by-slice writing must not drop, duplicate, or reorder chunks."""
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(
        db_session, source, filename="doc.txt", data=_LONG_TEXT.encode(), embedder=embedder
    )

    assert source.status is SourceStatus.DONE
    chunks = await _chunks_for(db_session, source)
    assert len(chunks) == source.chunks_total
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert len({c.ordinal for c in chunks}) == len(chunks)


async def test_reingest_shrinking_document_drops_the_surplus_tail(
    db_session: AsyncSession,
) -> None:
    """Reuse is a prefix: chunks past the new document's end must not linger."""
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)
    await ingest_source(
        db_session, source, filename="doc.txt", data=_LONG_TEXT.encode(), embedder=embedder
    )
    long_total = source.chunks_total

    shorter = _LONG_TEXT[: len(_LONG_TEXT) // 4].encode()
    await ingest_source(db_session, source, filename="doc.txt", data=shorter, embedder=embedder)

    assert source.status is SourceStatus.DONE
    assert long_total is not None and source.chunks_total is not None
    assert source.chunks_total < long_total
    chunks = await _chunks_for(db_session, source)
    assert len(chunks) == source.chunks_total


async def test_ingest_failure_is_logged(
    db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    """The message is stored on the source, but it must reach the logs too —
    otherwise a production failure is invisible in `heroku logs`."""
    _, source = await _make_source(db_session)
    with caplog.at_level("ERROR"):
        await ingest_source(
            db_session, source, filename="doc.txt", data=b"hello", embedder=BoomEmbedder()
        )
    assert source.status is SourceStatus.FAILED
    assert any(str(source.id) in record.getMessage() for record in caplog.records)
    assert any("embed exploded" in record.getMessage() for record in caplog.records)


async def _make_answerable_image_source(session: AsyncSession) -> Source:
    """A repo with a verified provider key and model, plus a pending image source."""
    repo = Repository(
        name="Vault",
        llm_provider=LLMProviderName.ANTHROPIC,
        llm_model="claude-x",
    )
    session.add(repo)
    session.add(
        ProviderSetting(
            provider=LLMProviderName.ANTHROPIC,
            api_key_encrypted=encrypt("secret-key"),
            verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.IMAGE,
        title="photo.heic",
        original_filename="photo.heic",
    )
    session.add(source)
    await session.flush()
    return source


async def test_image_ocr_releases_db_connection_during_transcription(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Image OCR must not pin a pooled DB connection across the slow vision call.

    Regression for the QueuePool exhaustion under bulk image upload: ``_ocr_image``
    used to hold the read transaction opened while loading the repo/key open across
    ``transcribe_image``, so N concurrent image ingestions pinned N connections for
    the whole OCR + embed. The connection must be released before the slow call.
    """
    source = await _make_answerable_image_source(db_session)

    in_transaction_during_ocr: dict[str, bool] = {}

    async def fake_transcribe(
        provider: str, api_key: str, model: str, *, image: bytes, base_url: str | None = None
    ) -> str:
        in_transaction_during_ocr["value"] = db_session.in_transaction()
        return "transcribed page text"

    monkeypatch.setattr("contextvault.services.ingestion.transcribe_image", fake_transcribe)
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(
        db_session, source, filename="photo.heic", data=b"rawimagebytes", embedder=embedder
    )

    assert source.status is SourceStatus.DONE
    assert in_transaction_during_ocr.get("value") is False, (
        "the DB connection must be released before the slow OCR call, not held across it"
    )


async def _make_answerable_custom_image_source(session: AsyncSession, base_url: str) -> Source:
    """A repo on a verified, keyless custom provider, plus a pending image source."""
    repo = Repository(
        name="Vault",
        llm_provider=LLMProviderName.CUSTOM,
        llm_model="llava",
    )
    session.add(repo)
    session.add(
        ProviderSetting(
            provider=LLMProviderName.CUSTOM,
            api_key_encrypted=None,
            base_url=base_url,
            verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    )
    await session.flush()
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.IMAGE,
        title="photo.heic",
        original_filename="photo.heic",
    )
    session.add(source)
    await session.flush()
    return source


async def test_image_ocr_on_keyless_custom_provider_transcribes_successfully(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A keyless custom (OpenAI-compatible) endpoint OCRs an image successfully.

    ``_ocr_image`` must not assert a non-null key: a verified custom provider may
    have no stored key at all, only a base URL. The base URL must still reach
    ``transcribe_image`` so the vision call is aimed at the right server.
    """
    base_url = "http://localhost:11434/v1"
    source = await _make_answerable_custom_image_source(db_session, base_url)

    captured: dict[str, str | None] = {}

    async def fake_transcribe(
        provider: str, api_key: str, model: str, *, image: bytes, base_url: str | None = None
    ) -> str:
        captured["base_url"] = base_url
        return "transcribed page text"

    monkeypatch.setattr("contextvault.services.ingestion.transcribe_image", fake_transcribe)
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(
        db_session, source, filename="photo.heic", data=b"rawimagebytes", embedder=embedder
    )

    assert source.status is SourceStatus.DONE
    assert source.ingest_error is None
    assert captured.get("base_url") == base_url


async def test_embedding_failure_is_recorded(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)

    await ingest_source(
        db_session, source, filename="doc.txt", data=b"hello", embedder=BoomEmbedder()
    )

    assert source.status is SourceStatus.FAILED
    assert source.ingest_error is not None
    assert "embed exploded" in source.ingest_error
    assert await _chunks_for(db_session, source) == []


async def test_parse_failure_is_recorded(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(db_session, source, filename="doc.xyz", data=b"anything", embedder=embedder)

    assert source.status is SourceStatus.FAILED
    assert source.ingest_error  # the UnsupportedDocumentError was captured
    assert await _chunks_for(db_session, source) == []


async def test_reingest_replaces_prior_chunks(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await ingest_source(
        db_session, source, filename="doc.txt", data=b"first body " * 200, embedder=embedder
    )
    first_ids = {c.id for c in await _chunks_for(db_session, source)}
    assert first_ids

    await ingest_source(
        db_session, source, filename="doc.txt", data=b"short second", embedder=embedder
    )
    second = await _chunks_for(db_session, source)
    second_ids = {c.id for c in second}

    assert source.status is SourceStatus.DONE
    assert first_ids.isdisjoint(second_ids)  # prior chunks were replaced, not appended
    assert len(second) == 1
    assert second[0].content == "short second"


async def test_run_ingestion_missing_source_is_noop(db_session: AsyncSession) -> None:
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await run_ingestion(
        uuid.uuid4(),
        filename="x.txt",
        data=b"hi",
        embedder=embedder,
        session_factory=_fixed_factory(db_session),
    )

    count = (await db_session.execute(sa.select(sa.func.count()).select_from(Chunk))).scalar_one()
    assert count == 0


async def test_run_ingestion_delegates_to_pipeline(db_session: AsyncSession) -> None:
    _, source = await _make_source(db_session)
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await run_ingestion(
        source.id,
        filename="doc.txt",
        data=b"body text here",
        embedder=embedder,
        session_factory=_fixed_factory(db_session),
    )

    refreshed = await db_session.get(Source, source.id)
    assert refreshed is not None
    assert refreshed.status is SourceStatus.DONE
    assert len(await _chunks_for(db_session, source)) == 1


async def test_run_web_ingestion_stores_extracted_text(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(name="Vault")
    db_session.add(repo)
    await db_session.flush()
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.WEB,
        title="https://x.test",
        source_url="https://x.test",
    )
    db_session.add(source)
    await db_session.flush()

    monkeypatch.setattr("contextvault.services.ingestion.fetch_html", lambda url: "<html/>")
    monkeypatch.setattr(
        "contextvault.services.ingestion.extract_web_text",
        lambda html: ("Extracted body text.", "Nice Title"),
    )
    embedder = FakeEmbedder(get_settings().embedding_dim)

    await run_web_ingestion(
        source.id,
        url="https://x.test",
        embedder=embedder,
        session_factory=_fixed_factory(db_session),
    )

    refreshed = await db_session.get(Source, source.id)
    assert refreshed is not None
    assert refreshed.status is SourceStatus.DONE
    assert refreshed.title == "Nice Title"
    assert refreshed.content == "Extracted body text."
    count = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Chunk).where(Chunk.source_id == source.id)
    )
    assert count is not None and count >= 1


async def test_run_web_ingestion_empty_text_fails(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(name="Vault2")
    db_session.add(repo)
    await db_session.flush()
    source = Source(
        repository_id=repo.id,
        kind=SourceKind.WEB,
        title="https://y.test",
        source_url="https://y.test",
    )
    db_session.add(source)
    await db_session.flush()

    monkeypatch.setattr("contextvault.services.ingestion.fetch_html", lambda url: "<html/>")
    monkeypatch.setattr(
        "contextvault.services.ingestion.extract_web_text", lambda html: ("   ", None)
    )

    await run_web_ingestion(
        source.id,
        url="https://y.test",
        embedder=FakeEmbedder(get_settings().embedding_dim),
        session_factory=_fixed_factory(db_session),
    )

    refreshed = await db_session.get(Source, source.id)
    assert refreshed is not None
    assert refreshed.status is SourceStatus.FAILED
    assert "No readable text" in (refreshed.ingest_error or "")
