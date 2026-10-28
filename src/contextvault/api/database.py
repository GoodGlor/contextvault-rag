"""Admin database-connection API — connect, introspect, allow-list, delete.

An admin points a repository at read-only external SQL databases (DB-reports
spec). **A repository holds several (#201)**, so these are collection routes
under ``/repositories/{repository_id}/databases``; they replaced the singular
``/database`` ones outright rather than living beside them, because both the
client and the server are in this repo and two shapes would be two code paths
to keep honest.

``POST`` and ``PUT`` live-test the connection before storing it (the goal:
never store a connection that doesn't work), the password is Fernet ciphertext
(``core/crypto``) and is **never** returned by any route, and the exposed-schema
allow-list — also what the report LLM is shown — can be edited afterward
(``PATCH .../schema``) without re-testing the connection.
``POST .../introspect`` reads live tables/columns from the stored connection so
the admin has something to annotate into that allow-list.

**Every per-connection route resolves the connection by (repository, id), never
by id alone.** The id is the only thing a caller supplies and it is not secret,
so looking it up bare would let any admin path reach any repository's
connection; the pair is the identity.
"""

import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import require_admin
from contextvault.core.crypto import decrypt, encrypt
from contextvault.db.session import get_session
from contextvault.llm.textgen import TextGenError
from contextvault.models import DatabaseConnection, DatabaseType, Repository, User
from contextvault.services import providers as provider_service
from contextvault.services.connection_description import (
    description_is_current,
    run_connection_description,
)
from contextvault.services.report_db import DBConnectionError, introspect_schema, test_connection
from contextvault.services.report_llm import (
    MAX_DESCRIPTION_CHARS,
    DescriptionUnavailable,
    generate_connection_description,
    schema_fingerprint,
)

router = APIRouter(tags=["database"])


class DatabaseConnectionRequest(BaseModel):
    """Admin-supplied connection details.

    ``password`` is optional on update — omitted or empty keeps the currently
    stored (encrypted) password rather than wiping it, so an admin can fix a
    typo'd host without re-entering the secret. It is required the first time a
    repository's connection is created (there is nothing stored yet to keep).

    ``exposed_schema`` is likewise optional on update — omitted (``None``)
    keeps the currently stored allow-list rather than wiping it, so an admin
    can fix a typo'd host without re-curating the allow-list set earlier via
    introspect + ``PATCH .../schema``. Only an explicit list (including ``[]``)
    replaces it.
    """

    name: str = Field(min_length=1, max_length=255)
    db_type: DatabaseType
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = ""
    exposed_schema: list[dict[str, Any]] | None = None


class SchemaUpdateRequest(BaseModel):
    """The admin's edited allow-list — also what the report LLM is shown."""

    exposed_schema: list[dict[str, Any]]
    # The interface language of the admin doing the saving (#215). Carried on
    # this request because saving is what queues the description (#212), and a
    # background task has no reader to ask. Optional and unvalidated here on
    # purpose: ``resolve_language`` degrades an absent, regional or nonsense
    # value rather than failing a save over a locale string.
    language: str | None = None


class DescriptionUpdateRequest(BaseModel):
    """A hand-written or hand-corrected description (#202).

    Correctable by hand is not a convenience: #203 routes a report's sections
    using this text, so a wrong generated description silently misroutes a
    report and an admin has to be able to fix it. Blank is allowed — clearing a
    wrong description is better than keeping one.
    """

    # Shares the generated cap (#215): a correction of a generated description
    # must not be rejected for being the same length as what it replaces.
    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)


class DatabaseConnectionResponse(BaseModel):
    """A stored connection as the admin manages it. The password is never
    included — only ``GET`` needs to prove that, but every route returns this
    shape."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    description_stale: bool
    db_type: DatabaseType
    host: str
    port: int
    database: str
    username: str
    exposed_schema: list[dict[str, Any]]


class IntrospectResponse(BaseModel):
    """Live tables/columns read from the stored connection (descriptions empty,
    for the admin to fill in and save via ``PATCH .../schema``)."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: list[dict[str, Any]] = Field(alias="schema")


def _response(conn: DatabaseConnection) -> DatabaseConnectionResponse:
    """The wire shape, with ``description_stale`` derived rather than stored.

    Derived in one place so no route can return a connection without deciding
    it. **Absent is not out-of-date**: a connection nobody has described yet is
    not stale, or every new connection would wear a warning before anyone had
    done anything wrong.
    """
    stale = bool(conn.description) and conn.description_schema_hash != schema_fingerprint(
        conn.exposed_schema
    )
    return DatabaseConnectionResponse(
        id=conn.id,
        name=conn.name,
        description=conn.description,
        description_stale=stale,
        db_type=conn.db_type,
        host=conn.host,
        port=conn.port,
        database=conn.database,
        username=conn.username,
        exposed_schema=conn.exposed_schema,
    )


async def _get_repo(session: AsyncSession, repository_id: uuid.UUID) -> Repository:
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    return repo


async def _get_connection(
    session: AsyncSession, repository_id: uuid.UUID, connection_id: uuid.UUID
) -> DatabaseConnection | None:
    """Resolve by the (repository, connection) pair — see the module docstring."""
    result = await session.execute(
        select(DatabaseConnection).where(
            DatabaseConnection.repository_id == repository_id,
            DatabaseConnection.id == connection_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_connection_or_404(
    session: AsyncSession, repository_id: uuid.UUID, connection_id: uuid.UUID
) -> DatabaseConnection:
    conn = await _get_connection(session, repository_id, connection_id)
    if conn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Database connection not found"
        )
    return conn


async def _live_test(payload: DatabaseConnectionRequest, password: str) -> None:
    try:
        await test_connection(
            db_type=payload.db_type,
            host=payload.host,
            port=payload.port,
            database=payload.database,
            username=payload.username,
            password=password,
        )
    except DBConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


async def _refuse_duplicate_name(
    session: AsyncSession,
    repository_id: uuid.UUID,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """409 if this repository already has a connection called ``name``.

    **Checked before the write, not caught after it — deliberately, and with a
    known limit.** ``uq_database_connections_repository_name`` is what actually
    guarantees uniqueness, and letting it fire would be immune to the
    check-then-write race this has. But recovering from a constraint violation
    means unwinding the transaction: Postgres aborts it outright, and neither
    escape works here. A plain ``session.rollback()`` unwinds whatever
    transaction the session is in — under the test fixture that is a
    caller-owned outer transaction, so the 409 would leave the session unusable
    for everything after it. A ``SAVEPOINT`` via ``begin_nested`` does not help
    either: a *failed flush* marks the session's own transaction invalid, so the
    next statement still raises ``PendingRollbackError``.

    So the check races, and losing it means the constraint fires and the admin
    gets a 500 instead of a sentence. That window is two concurrent admins
    naming a connection identically in the same repository in the same
    milliseconds; the data stays correct either way, because the constraint —
    not this function — is what enforces it.

    ``exclude_id`` lets an update keep its own name: a rename that changes
    nothing else must not collide with itself.
    """
    stmt = select(DatabaseConnection.id).where(
        DatabaseConnection.repository_id == repository_id,
        DatabaseConnection.name == name,
    )
    if exclude_id is not None:
        stmt = stmt.where(DatabaseConnection.id != exclude_id)
    if (await session.execute(stmt)).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A database connection with this name already exists in this repository",
        )


@router.get("/repositories/{repository_id}/databases")
async def list_database_connections(
    repository_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[DatabaseConnectionResponse]:
    """Every connection this repository holds — never a password.

    Ordered by name so a picker is stable between calls; insertion order is not
    something an admin can predict or reorder.
    """
    await _get_repo(session, repository_id)
    result = await session.execute(
        select(DatabaseConnection)
        .where(DatabaseConnection.repository_id == repository_id)
        .order_by(DatabaseConnection.name)
    )
    return [_response(c) for c in result.scalars()]


@router.post("/repositories/{repository_id}/databases", status_code=status.HTTP_201_CREATED)
async def create_database_connection(
    repository_id: uuid.UUID,
    payload: DatabaseConnectionRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DatabaseConnectionResponse:
    """Live-test then store a new connection (never store one that
    doesn't work). ``test_connection`` runs first and a
    :class:`DBConnectionError` becomes a 400 with its detail before anything is
    written. A password is required — there is no stored one to keep."""
    await _get_repo(session, repository_id)

    if not payload.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="password is required to create a new connection",
        )
    await _refuse_duplicate_name(session, repository_id, payload.name)
    await _live_test(payload, payload.password)

    conn = DatabaseConnection(repository_id=repository_id, created_by=admin.id)
    conn.name = payload.name
    conn.db_type = payload.db_type
    conn.host = payload.host
    conn.port = payload.port
    conn.database = payload.database
    conn.username = payload.username
    conn.password_encrypted = encrypt(payload.password)
    conn.exposed_schema = payload.exposed_schema or []
    session.add(conn)

    await session.commit()
    await session.refresh(conn)
    return _response(conn)


@router.get("/repositories/{repository_id}/databases/{connection_id}")
async def get_database_connection(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DatabaseConnectionResponse:
    """Read one stored connection — never the password. 404 when the repository
    doesn't exist, the connection doesn't exist, or it belongs to a different
    repository."""
    await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)
    return _response(conn)


@router.put("/repositories/{repository_id}/databases/{connection_id}")
async def update_database_connection(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    payload: DatabaseConnectionRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DatabaseConnectionResponse:
    """Live-test then update a stored connection.

    An omitted/empty password keeps the stored one, and an omitted
    ``exposed_schema`` keeps the stored allow-list rather than wiping it — so an
    admin can fix a typo'd host without re-entering the secret or re-curating
    the allow-list.
    """
    await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)

    await _refuse_duplicate_name(session, repository_id, payload.name, exclude_id=connection_id)
    password = payload.password or decrypt(conn.password_encrypted)
    await _live_test(payload, password)

    if payload.exposed_schema is not None:
        conn.exposed_schema = payload.exposed_schema
    conn.name = payload.name
    conn.db_type = payload.db_type
    conn.host = payload.host
    conn.port = payload.port
    conn.database = payload.database
    conn.username = payload.username
    conn.password_encrypted = encrypt(password)

    await session.commit()
    await session.refresh(conn)
    return _response(conn)


@router.patch("/repositories/{repository_id}/databases/{connection_id}/schema")
async def update_exposed_schema(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    payload: SchemaUpdateRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DatabaseConnectionResponse:
    """Save the admin's edited exposed-schema allow-list without re-testing the
    connection (only ``POST``/``PUT`` verify reachability).

    **Saving is also what makes the connection describe itself (#212).** The
    description is queued as a background task rather than generated here: #202
    refused to generate inline because it would make every allow-list edit wait
    on a provider round-trip and fail when one is down, and both of those stay
    true — this function commits and returns before the task runs.

    The condition is the fingerprint, which already means "no description of
    *this* allow-list": true for a connection nobody has described and for one
    whose tables just changed, false for a re-save that changed nothing, so
    re-saving the same curation spends no provider call.
    """
    await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)
    conn.exposed_schema = payload.exposed_schema
    await session.commit()
    await session.refresh(conn)
    if conn.exposed_schema and not description_is_current(conn):
        background_tasks.add_task(run_connection_description, conn.id, payload.language)
    return _response(conn)


@router.delete(
    "/repositories/{repository_id}/databases/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_database_connection(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove one connection; the generated reports and schedules that name it
    cascade away with it (FK ``ON DELETE CASCADE``, unchanged by #201). The
    repository's other connections and their reports are untouched."""
    await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)
    await session.delete(conn)
    await session.commit()


@router.post("/repositories/{repository_id}/databases/{connection_id}/introspect")
async def introspect_database(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> IntrospectResponse:
    """Live-read tables/columns from one stored connection, for the admin to
    annotate and save as the exposed-schema allow-list (``PATCH .../schema``).

    404 when the connection is not this repository's — the route now names a
    specific connection, so "which one" is a lookup failure rather than the
    "nothing is configured" 400 the singular route answered with. 400 remains
    for a stored connection that cannot be reached.
    """
    await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)
    try:
        tables = await introspect_schema(
            db_type=conn.db_type,
            host=conn.host,
            port=conn.port,
            database=conn.database,
            username=conn.username,
            password=decrypt(conn.password_encrypted),
        )
    except DBConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return IntrospectResponse(schema=tables)


@router.post("/repositories/{repository_id}/databases/{connection_id}/describe")
async def describe_database_connection(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    language: str | None = None,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DatabaseConnectionResponse:
    """Generate this connection's one-line description from its allow-list (#202).

    Explicit rather than automatic, and explicitly *not* folded into
    ``PATCH .../schema``: generating there would make every allow-list save wait
    on a provider round-trip and fail when one is down, turning a fast local edit
    into a slow, quota-dependent one.

    **Failure never damages anything.** A repository with no usable model, or a
    provider that refuses, is a 400 carrying the reason — matching how
    ``ModelListError`` already surfaces — and the connection keeps its
    allow-list, its previous description, and its usability. Nothing about
    querying a database depends on it having been described.
    """
    repo = await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)

    if not await provider_service.repo_is_answerable(session, repo):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This repository has no model with a verified key; "
                "pick one in the repository's LLM settings first."
            ),
        )
    if not conn.exposed_schema:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Introspect the database and save an allow-list first — "
                "there is nothing to describe yet."
            ),
        )

    assert repo.llm_provider is not None  # repo_is_answerable proved it
    api_key, base_url = await provider_service.get_call_credentials(session, repo.llm_provider)
    try:
        description = await generate_connection_description(
            repo.llm_provider.value,
            api_key,
            repo.llm_model or "",
            base_url=base_url,
            exposed_schema=conn.exposed_schema,
            db_type=conn.db_type,
            language=language,
        )
    except (TextGenError, DescriptionUnavailable) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    conn.description = description
    conn.description_schema_hash = schema_fingerprint(conn.exposed_schema)
    await session.commit()
    await session.refresh(conn)
    return _response(conn)


@router.patch("/repositories/{repository_id}/databases/{connection_id}/description")
async def update_description(
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    payload: DescriptionUpdateRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DatabaseConnectionResponse:
    """Correct a description by hand, without re-testing the connection.

    Mirrors ``PATCH .../schema``. The fingerprint is stamped to the allow-list as
    it stands now: a human writing a description is describing what they are
    looking at, so it starts current rather than immediately stale.
    """
    await _get_repo(session, repository_id)
    conn = await _get_connection_or_404(session, repository_id, connection_id)
    conn.description = payload.description.strip()
    conn.description_schema_hash = (
        schema_fingerprint(conn.exposed_schema) if conn.description else ""
    )
    await session.commit()
    await session.refresh(conn)
    return _response(conn)
