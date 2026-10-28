"""Admin source management: upload documents, view ingestion status, list/delete.

Admin-facing endpoints. Uploading a document creates a
``Source`` (status ``pending``) and schedules the ingestion pipeline
as a FastAPI background task, so the request returns immediately while
parse→chunk→embed→store runs behind it. Status is then observable on the source.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import (
    get_current_user,
    get_embedder,
    get_ingestion_session_factory,
    require_admin,
)
from contextvault.db.session import get_session
from contextvault.embeddings.base import EmbeddingProvider
from contextvault.ingestion import IMAGE_SUFFIXES, file_suffix
from contextvault.models import Repository, Source, SourceKind, SourceStatus, User
from contextvault.services import grants as grant_service
from contextvault.services import providers as provider_service
from contextvault.services.ingestion import SessionFactory, run_ingestion, run_web_ingestion

router = APIRouter(tags=["sources"])

# Admin Notes are ingested through the same parse→chunk→embed pipeline as uploads by
# presenting their body as a plain-text document (the filename is not persisted).
_ADMIN_NOTE_FILENAME = "admin-note.txt"


class SourceResponse(BaseModel):
    """A source with its ingestion state, for the admin UI."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository_id: uuid.UUID
    kind: SourceKind
    title: str
    original_filename: str | None
    source_url: str | None
    status: SourceStatus
    ingest_error: str | None
    # Embed-stage progress for the admin UI's bar. Null on sources ingested before
    # progress tracking existed, and on kinds that never reach the embed stage yet.
    chunks_total: int | None
    chunks_done: int | None
    created_at: datetime


@router.post(
    "/repositories/{repository_id}/sources",
    status_code=status.HTTP_201_CREATED,
)
async def upload_source(
    repository_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    embedder: EmbeddingProvider = Depends(get_embedder),
    session_factory: SessionFactory = Depends(get_ingestion_session_factory),
) -> SourceResponse:
    """Upload a document to a repository and kick off ingestion in the background."""
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    data = await file.read()
    filename = file.filename or "untitled"
    suffix = file_suffix(filename)
    kind = SourceKind.IMAGE if suffix in IMAGE_SUFFIXES else SourceKind.DOCUMENT

    # Images are OCR'd by the repository's own vision model, so the repo must be
    # answerable (a model picked whose provider has a verified key) before we accept
    # one — fail fast here rather than let the upload land and its ingestion fail.
    if kind is SourceKind.IMAGE and not await provider_service.repo_is_answerable(session, repo):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Configure a model for this repository before uploading images: pick a "
                "model whose provider has a verified API key."
            ),
        )
    source = Source(
        repository_id=repository_id,
        kind=kind,
        title=filename,
        original_filename=filename,
        status=SourceStatus.PENDING,
    )
    session.add(source)
    await session.commit()
    # Load the server-generated columns (id, created_at) before serializing.
    await session.refresh(source)

    background_tasks.add_task(
        run_ingestion,
        source.id,
        filename=filename,
        data=data,
        embedder=embedder,
        session_factory=session_factory,
    )
    return SourceResponse.model_validate(source)


class WebSourceRequest(BaseModel):
    """A URL to fetch and ingest as a single web-page source."""

    url: AnyHttpUrl


@router.post(
    "/repositories/{repository_id}/web-sources",
    status_code=status.HTTP_201_CREATED,
)
async def add_web_source(
    repository_id: uuid.UUID,
    payload: WebSourceRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    embedder: EmbeddingProvider = Depends(get_embedder),
    session_factory: SessionFactory = Depends(get_ingestion_session_factory),
) -> SourceResponse:
    """Add a single web page as a source: fetch + extract run in the background."""
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    url = str(payload.url)
    source = Source(
        repository_id=repository_id,
        kind=SourceKind.WEB,
        title=url[:512],
        source_url=url,
        status=SourceStatus.PENDING,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    background_tasks.add_task(
        run_web_ingestion,
        source.id,
        url=url,
        embedder=embedder,
        session_factory=session_factory,
    )
    return SourceResponse.model_validate(source)


class AdminNoteRequest(BaseModel):
    """An admin-authored answer to index as a first-class, verified source."""

    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


@router.post(
    "/repositories/{repository_id}/admin-notes",
    status_code=status.HTTP_201_CREATED,
)
async def create_admin_note(
    repository_id: uuid.UUID,
    payload: AdminNoteRequest,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    embedder: EmbeddingProvider = Depends(get_embedder),
    session_factory: SessionFactory = Depends(get_ingestion_session_factory),
) -> SourceResponse:
    """Write an Admin Note and index it (closes the gap
    flywheel). The note becomes an ``admin_note`` source, attributed to the author,
    and is ingested (chunk+embed) exactly like an upload so it is retrievable and
    cited as a first-class, *Verified* source. To answer a knowledge gap, the admin
    titles the note with the gap's question.
    """
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    source = Source(
        repository_id=repository_id,
        kind=SourceKind.ADMIN_NOTE,
        title=payload.title,
        content=payload.content,
        created_by=admin.id,
        status=SourceStatus.PENDING,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    background_tasks.add_task(
        run_ingestion,
        source.id,
        filename=_ADMIN_NOTE_FILENAME,
        data=f"{payload.title}\n\n{payload.content}".encode(),
        embedder=embedder,
        session_factory=session_factory,
    )
    return SourceResponse.model_validate(source)


class SourceContentResponse(BaseModel):
    """A source's passage text, for a granted user reading a citation."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository_id: uuid.UUID
    title: str
    kind: SourceKind
    content: str | None


@router.get("/repositories/{repository_id}/sources/{source_id}")
async def read_source_content(
    repository_id: uuid.UUID,
    source_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SourceContentResponse:
    """Read a cited source's passage text. Any authenticated user, gated by
    an **active grant** on the repository (`403` without — the same rule retrieval
    enforces). `404` if the source is not in this repository."""
    if not await grant_service.has_active_grant(session, user.id, repository_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )
    source = await session.get(Source, source_id)
    if source is None or source.repository_id != repository_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return SourceContentResponse.model_validate(source)


@router.get("/repositories/{repository_id}/sources")
async def list_sources(
    repository_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[SourceResponse]:
    """List the sources in a repository, oldest first."""
    result = await session.execute(
        select(Source).where(Source.repository_id == repository_id).order_by(Source.created_at)
    )
    return [SourceResponse.model_validate(s) for s in result.scalars().all()]


@router.get("/sources/{source_id}")
async def get_source(
    source_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    """Fetch a single source, including its ingestion status."""
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return SourceResponse.model_validate(source)


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a source; its chunks cascade away with it."""
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    await session.delete(source)
    await session.commit()
