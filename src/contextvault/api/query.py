"""The query endpoint — the full RAG loop behind one route.

`POST /repositories/{id}/query` is where everything built so far meets the user:
authenticate, enforce the repository grant, retrieve the access-filtered chunks,
generate a grounded answer through the configured provider, and return the answer
with its citations resolved to real source documents.

Two access checks stand in front of generation. The repository must exist (404),
and the caller must hold an *active* grant on it (403) — the same predicate the
retrieval query enforces at the SQL level, surfaced here as an explicit,
first-class denial rather than an empty result. Beyond that gate the honest
"not in this vault" behaviour carries through untouched: weak or empty
retrieval flows into the provider, which returns the flagged refusal, so the
endpoint never special-cases it.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import RepoLLMBuilder, get_current_user, get_embedder, get_llm_builder
from contextvault.db.session import get_session
from contextvault.embeddings.base import EmbeddingProvider
from contextvault.llm import Citation
from contextvault.models import Repository, Source, SourceKind, User
from contextvault.retrieval import retrieve
from contextvault.services import conversations as convo_service
from contextvault.services import grants as grant_service
from contextvault.services import providers as provider_service
from contextvault.services.query_log import log_query

router = APIRouter(tags=["query"])


# Cap how many prior turns are threaded into a request, so a long conversation
# can't grow the prompt (and token cost) without bound. The most recent turns
# carry the most relevant context, so we keep the tail.
MAX_HISTORY_TURNS = 10


class QueryRequest(BaseModel):
    """A user's question against one repository.

    Conversation history is server-side: the endpoint loads this user's saved
    thread for the repository and threads the most recent ``MAX_HISTORY_TURNS``
    into the prompt. Clients send only the question.
    """

    question: str = Field(min_length=1)


class CitationResponse(BaseModel):
    """One ``[n]`` citation resolved to its source span, for the UI to jump to."""

    model_config = ConfigDict(from_attributes=True)

    number: int
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    char_start: int | None
    char_end: int | None


class SourceReferenceResponse(BaseModel):
    """A cited source document, so the client can label and link each citation.

    ``verified`` marks an **Admin Note** — a human-authored answer — so
    the UI can show a *Verified* badge; ``author`` is the admin's nickname it is
    cited to (null once that admin is deleted, or for uploaded documents).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    original_filename: str | None
    kind: SourceKind
    verified: bool
    author: str | None


class QueryResponse(BaseModel):
    """The RAG result: the answer, its honesty flag, citations, and their sources."""

    answer: str
    not_in_vault: bool
    citations: list[CitationResponse]
    sources: list[SourceReferenceResponse]


async def _cited_sources(
    session: AsyncSession, citations: list[Citation]
) -> list[SourceReferenceResponse]:
    """Load the distinct sources the citations point at, in first-cited order.

    Admin Notes are flagged ``verified`` and attributed to their author's
    nickname, so the UI can render a *Verified* badge and "by <admin>".
    """
    ordered_ids: list[uuid.UUID] = []
    for citation in citations:
        if citation.source_id not in ordered_ids:
            ordered_ids.append(citation.source_id)
    if not ordered_ids:
        return []

    rows = (await session.execute(select(Source).where(Source.id.in_(ordered_ids)))).scalars().all()
    by_id = {source.id: source for source in rows}

    # Resolve author nicknames for the cited Admin Notes in one query.
    author_ids = {s.created_by for s in rows if s.kind is SourceKind.ADMIN_NOTE and s.created_by}
    authors: dict[uuid.UUID, str] = {}
    if author_ids:
        author_rows = (
            (await session.execute(select(User).where(User.id.in_(author_ids)))).scalars().all()
        )
        authors = {u.id: u.username for u in author_rows}

    references: list[SourceReferenceResponse] = []
    for source_id in ordered_ids:
        source = by_id.get(source_id)
        if source is None:
            continue
        verified = source.kind is SourceKind.ADMIN_NOTE
        author = authors.get(source.created_by) if verified and source.created_by else None
        references.append(
            SourceReferenceResponse(
                id=source.id,
                title=source.title,
                original_filename=source.original_filename,
                kind=source.kind,
                verified=verified,
                author=author,
            )
        )
    return references


@router.post("/repositories/{repository_id}/query")
async def query_repository(
    repository_id: uuid.UUID,
    payload: QueryRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    embedder: EmbeddingProvider = Depends(get_embedder),
    build_provider: RepoLLMBuilder = Depends(get_llm_builder),
) -> QueryResponse:
    """Answer a question against one repository: retrieve → generate → cite.

    Requires an active grant on the repository; without one the caller is denied
    (403) rather than shown an empty result. Retrieval is access-filtered and
    thresholded, so an out-of-corpus question yields no chunks and the provider
    returns the honest "not in this vault" answer.
    """
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    if not await grant_service.has_active_grant(session, user.id, repository_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )
    # A repository must be answerable before it can generate (no
    # system default). That means a model is picked *and* its provider has a verified
    # key in the global provider settings. Generation then routes to that provider,
    # built from the repo's chosen provider/model plus the shared key.
    if not await provider_service.repo_is_answerable(session, repo):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Repository has no usable LLM; an admin must pick a model whose "
                "provider has a verified API key before it can answer."
            ),
        )
    provider = await build_provider(session, repo)

    conversation = await convo_service.get_or_create_conversation(session, user.id, repository_id)
    history = await convo_service.recent_history(session, conversation.id, MAX_HISTORY_TURNS)

    # Contextualise retrieval for follow-ups: a terse question like "and for
    # part-timers?" embeds poorly alone, so prepend the previous question so the
    # vector search lands on the same topic. This shapes retrieval only — the
    # answered and logged question stays the raw one the user typed.
    retrieval_question = payload.question
    if history:
        retrieval_question = f"{history[-1][0]}\n{payload.question}"

    result = await retrieve(
        session,
        question=retrieval_question,
        repository_id=repository_id,
        user_id=user.id,
        embedder=embedder,
    )
    answer = await provider.answer(payload.question, result.chunks, history)

    citation_responses = [CitationResponse.model_validate(c) for c in answer.citations]
    source_references = await _cited_sources(session, answer.citations)

    # Log the query — the raw material for the gap dashboard (#31) and analytics
    # (#33): who asked, against which repo, the retrieval signal, and whether the
    # answer was grounded. Persisted before returning so nothing is lost.
    await log_query(
        session,
        user_id=user.id,
        repository_id=repository_id,
        question=payload.question,
        top_score=result.top_score,
        chunk_count=len(result.chunks),
        not_in_vault=answer.not_in_vault,
    )
    # Append this exchange to the saved conversation, so a follow-up question (or
    # a page reload) sees it as server-authoritative history (the
    # server, not the client, owns conversation history).
    await convo_service.append_turn(
        session,
        conversation.id,
        question=payload.question,
        answer=answer.text,
        not_in_vault=answer.not_in_vault,
        citations=[c.model_dump(mode="json") for c in citation_responses],
        sources=[s.model_dump(mode="json") for s in source_references],
    )
    await session.commit()

    return QueryResponse(
        answer=answer.text,
        not_in_vault=answer.not_in_vault,
        citations=citation_responses,
        sources=source_references,
    )
