"""Documents-section executor: grounded answer, honest empty, missing-key error."""

import uuid
from collections.abc import Sequence

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import contextvault.services.report_documents as docs_module
from contextvault.llm.base import Answer, Citation
from contextvault.models import Repository, Source, SourceKind, SourceStatus
from contextvault.retrieval import RetrievalResult, RetrievedChunk
from contextvault.services.report_documents import (
    EMBEDDINGS_UNAVAILABLE,
    NO_DOCUMENTS_NOTE,
    execute_documents_section,
)
from contextvault.services.report_planner import PlannedSection

PLANNED = PlannedSection(title="Політика", source="documents", sub_question="правила відпусток")


class _FakeLLM:
    def __init__(self, answer: Answer) -> None:
        self._answer = answer
        self.questions: list[str] = []

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        self.questions.append(question)
        return self._answer


def _chunk(source_id: uuid.UUID) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        source_id=source_id,
        repository_id=uuid.uuid4(),
        ordinal=0,
        content="Відпустка триває 24 дні.",
        char_start=0,
        char_end=24,
        score=0.9,
    )


def _stub_common(monkeypatch: pytest.MonkeyPatch, *, gemini_key: str | None) -> None:
    async def fake_get_key(session: object, provider: object) -> str | None:
        return gemini_key

    monkeypatch.setattr(docs_module.provider_service, "get_provider_key", fake_get_key)
    monkeypatch.setattr(
        docs_module,
        "GeminiEmbeddingProvider",
        lambda **kwargs: object(),  # never actually embeds — retrieve is stubbed too
    )


async def test_grounded_answer_with_resolved_sources(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = Repository(name="Vault")
    db_session.add(repo)
    await db_session.flush()
    source = Source(
        repository_id=repo.id,
        title="policy.pdf",
        kind=SourceKind.DOCUMENT,
        status=SourceStatus.DONE,
    )
    db_session.add(source)
    await db_session.flush()

    _stub_common(monkeypatch, gemini_key="gk")
    chunk = _chunk(source.id)

    async def fake_retrieve(session: AsyncSession, **kwargs: object) -> RetrievalResult:
        assert kwargs["question"] == PLANNED.sub_question
        return RetrievalResult(chunks=[chunk], top_score=0.9)

    monkeypatch.setattr(docs_module, "retrieve", fake_retrieve)
    answer = Answer(
        text="Відпустка триває 24 дні [1].",
        citations=[
            Citation(
                number=1,
                chunk_id=chunk.chunk_id,
                source_id=source.id,
                char_start=0,
                char_end=24,
            )
        ],
    )
    fake_llm = _FakeLLM(answer)
    monkeypatch.setattr(docs_module, "get_llm_provider", lambda *a, **k: fake_llm)

    result = await execute_documents_section(
        db_session,
        repository_id=repo.id,
        requested_by=uuid.uuid4(),
        planned=PLANNED,
        provider_name="gemini",
        api_key="key",
        model="gem-1",
        base_url=None,
    )

    assert result.kind == "text"
    assert result.prose == "Відпустка триває 24 дні [1]."
    # The other side of the flag: a grounded answer is never empty, however few
    # sources it cites. Without this, marking everything would refuse reports
    # that had plenty to say (#185).
    assert result.empty is False
    assert [(s.number, s.title) for s in result.sources] == [(1, "policy.pdf")]
    assert fake_llm.questions == [PLANNED.sub_question]


async def test_empty_retrieval_yields_honest_note(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_common(monkeypatch, gemini_key="gk")

    async def fake_retrieve(session: AsyncSession, **kwargs: object) -> RetrievalResult:
        return RetrievalResult(chunks=[], top_score=None)

    monkeypatch.setattr(docs_module, "retrieve", fake_retrieve)

    result = await execute_documents_section(
        db_session,
        repository_id=uuid.uuid4(),
        requested_by=uuid.uuid4(),
        planned=PLANNED,
        provider_name="gemini",
        api_key="key",
        model="gem-1",
        base_url=None,
    )

    assert result.kind == "text"
    assert result.prose == NO_DOCUMENTS_NOTE
    assert result.sources == []
    # Marked, not inferred (#185). Downstream refuses a report whose sections all
    # came back empty, and it must not have to tell this note apart from a real
    # answer by comparing prose against a translated string — nor by an empty
    # `sources`, which a genuine uncited answer also has.
    assert result.empty is True


async def test_missing_gemini_key_yields_error_section(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_common(monkeypatch, gemini_key=None)

    result = await execute_documents_section(
        db_session,
        repository_id=uuid.uuid4(),
        requested_by=uuid.uuid4(),
        planned=PLANNED,
        provider_name="gemini",
        api_key="key",
        model="gem-1",
        base_url=None,
    )

    assert result.kind == "error"
    assert result.error == EMBEDDINGS_UNAVAILABLE


async def test_not_in_vault_answer_yields_honest_note(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_common(monkeypatch, gemini_key="gk")
    chunk = _chunk(uuid.uuid4())

    async def fake_retrieve(session: AsyncSession, **kwargs: object) -> RetrievalResult:
        return RetrievalResult(chunks=[chunk], top_score=0.5)

    monkeypatch.setattr(docs_module, "retrieve", fake_retrieve)
    refusal = Answer(text="not here", citations=[], not_in_vault=True)
    monkeypatch.setattr(docs_module, "get_llm_provider", lambda *a, **k: _FakeLLM(refusal))

    result = await execute_documents_section(
        db_session,
        repository_id=uuid.uuid4(),
        requested_by=uuid.uuid4(),
        planned=PLANNED,
        provider_name="gemini",
        api_key="key",
        model="gem-1",
        base_url=None,
    )

    assert result.kind == "text"
    assert result.prose == NO_DOCUMENTS_NOTE
    assert result.empty is True
