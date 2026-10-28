"""Database-backed reports API: request → plan
review → generate → poll → per-user history → PDF/PPTX download.

A report is asked for in natural language against a repository's connected
reporting database (``services/database.py`` / ``DatabaseConnection``) and/or its
ingested sources. ``POST .../reports`` creates a ``PLANNING`` row and dispatches
planning as a background task, returning immediately for the caller to poll via
``GET .../reports/{id}`` until it reaches ``AWAITING_APPROVAL`` with a ``plan`` on
the row. From there the caller drives a review loop: ``POST .../replan`` sends an
edited draft back for another round (the model fills only what was left blank,
everything the caller supplied survives verbatim) and returns to ``PLANNING``;
``POST .../approve`` re-validates the plan as it stands on the caller's screen and,
only from there, starts generation (NL→SQL/RAG→PDF) as a
background task. ``/approve`` is the only path that puts a report into
``PENDING`` — the service layer does not itself verify a plan was approved before
executing it, so that gate lives here in the router.

Access follows the same shape as the query endpoint (``api/query.py``): the
repository must exist (404) and, for a non-admin, an *active* grant is required
(403). For reports and schedules, admins bypass the grant check entirely — this
is specific to this module (``query.py``/``conversations.py``/``sources.py``
still apply ``has_active_grant`` to admins too).
Report *history* is per-user by default (``GET .../reports`` returns only the
caller's own rows); ``?all=true`` is an admin-only escape hatch onto every user's
reports for this repository, and only that admin view exposes the audit trail of
what ran: ``generated_sql`` for legacy single-SQL reports, and ``sections`` (the
per-section SQL / cited sources) for blended reports. A single report, its PDF download, and
its deletion are all owner-or-admin; a non-admin reaching for someone else's report
gets 404 (not 403), so the report's existence is never revealed to a stranger.
"""

import uuid
from datetime import datetime
from typing import Any, Literal, NamedTuple, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from sqlalchemy import Select, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from contextvault.api.deps import (
    ensure_known_template,
    get_current_user,
    get_repo_or_404,
    guard_repo_access,
)
from contextvault.db.session import get_session
from contextvault.models import (
    DatabaseConnection,
    GeneratedReport,
    ReportAsset,
    ReportStatus,
    Role,
    User,
)
from contextvault.services import grants as grant_service
from contextvault.services.report_blocks import (
    BLOCKS_VERSION,
    Block,
    BlockIn,
    allowed_chart_types,
    default_blocks_from_storage,
    draws_chart,
    load_blocks,
    validate_blocks,
)
from contextvault.services.report_design import design_payload, load_design
from contextvault.services.report_persist import load_sections
from contextvault.services.report_planner import (
    ReportPlan,
    ReportPlanDraft,
    ReportPlanParseError,
    check_sources_available,
    finalize_draft,
)
from contextvault.services.report_style import TEMPLATES
from contextvault.services.reports import (
    ReportNotRenderable,
    has_done_sources,
    preview_report_html,
    rerender_report,
    run_report_generation,
    run_report_planning,
    run_report_replanning,
)

router = APIRouter(tags=["reports"])

# Both plan endpoints reject the same way, from two places each (the read gate and
# the conditional write that closes the race after it), so the wording lives once.
_NOT_AWAITING = "This report is not awaiting approval"


class ReportRequest(BaseModel):
    """A natural-language report request against a repository's reporting database."""

    prompt: str = Field(min_length=1)


class ReportDesignRequest(BaseModel):
    """A reader's design for one report: its template, its block list, or both.

    Every field is optional and the endpoint is a *partial* update, so a client
    changing the template does not have to resend the block list. Omitting a key
    leaves it alone; sending it as ``null`` clears it.
    """

    template: str | None = None
    blocks_version: int | None = None
    blocks: list[BlockIn] | None = None
    # 5f. The editor's three inspector panels — Типографіка, Сторінка, Брендкіт.
    # Typed loosely on purpose: the read path is deliberately tolerant of a
    # value it does not recognise (``report_design.style_tokens_for``), so
    # rejecting one here would make the write stricter than the read for no
    # gain, and a token whose value is merely unknown is not a typo the caller
    # can be told about. ``brand.logo_asset_id`` IS checked below, because that
    # one is an access-control question rather than a styling preference.
    typography: dict[str, Any] | None = None
    page: dict[str, Any] | None = None
    brand: dict[str, Any] | None = None


class ReportSectionSummary(BaseModel):
    """What the editor needs to know about one section to offer choices about it.

    Deliberately not the section's data. The editor arranges blocks; it never
    reads a row, and shipping the result here would put megabytes behind a call
    made every time the editor opens.
    """

    index: int
    title: str
    kind: str
    # Sorted, so the chips render in a stable order without the SPA choosing one.
    # Derived from the stored result per parent design §3 — the reason a pie is
    # not offered for a time series is that this list does not contain it, not
    # that the frontend knows to leave it out.
    allowed_chart_types: list[str]
    # Whether the report as generated draws a figure for this section. Distinct
    # from ``allowed_chart_types`` being non-empty: a section the model declined
    # to chart draws nothing yet can support all three.
    draws_chart: bool
    # The words a ``prose`` and a ``sources`` block on this section render when
    # nothing overrides them. The editor seeds its fields from these — without
    # them the only way to show the reader what a block currently says would be to
    # read it back out of the PDF. Still not the section's *data*: a row is never
    # editable, so no row is sent (see the note above).
    prose: str | None
    # Pre-joined the way the renderers join it, rather than as a list, because an
    # override replaces the whole line: sending the parts would invite a second
    # opinion in the SPA about how they go together.
    sources: str | None


def _group(design: dict[str, Any] | None, name: str) -> dict[str, Any]:
    """One stored style group, or empty when it is absent or not a mapping.

    Tolerant for the same reason the render path is: a group written by a later
    version, or corrupted, must degrade to "nothing chosen" rather than 500 the
    editor that is trying to open the report and fix it.
    """
    value = (design or {}).get(name)
    return value if isinstance(value, dict) else {}


class ReportDesignResponse(BaseModel):
    """The editor's read model: the block list in force, and what it may become."""

    template: str | None
    # The document's stored name — what a ``cover`` block renders with no
    # override. Not ``document_title``'s answer: the editor needs the *original*
    # to seed the field and to show what dropping the override restores.
    title: str | None
    blocks_version: int
    # The list the report *renders from* — stored if there is one, derived
    # otherwise. Never empty for an editable report, which is what lets the
    # editor open on the document the reader can see rather than a blank tree.
    blocks: list[Block]
    # Whether ``blocks`` came off the row. "Unedited" and "edited into exactly
    # the default" are different facts, and only the first may be silently
    # replaced by a future default.
    stored: bool
    sections: list[ReportSectionSummary]
    # 5f — the three inspector panels, echoed back exactly as stored. The
    # editor's save path is "take what you gave me, change one field, send it
    # back", so a read that dropped or normalised a group would make a
    # round-trip lose the reader's choices.
    typography: dict[str, Any]
    page: dict[str, Any]
    brand: dict[str, Any]

    @field_serializer("blocks")
    def _compact_blocks(self, blocks: list[Block]) -> list[dict[str, Any]]:
        """Blocks go out in the shape they are stored in — defaults omitted.

        The symmetry is the point: the editor's save path is "take the list you
        gave me, move one entry, send it back", so a round-trip that changed
        nothing must write byte-identical JSON to the column. Serializing every
        default would also make the wire carry five nulls per block and force the
        SPA's mirror type to declare each one nullable rather than optional.
        """
        return [block.model_dump(exclude_defaults=True) for block in blocks]


class ReportResponse(BaseModel):
    """One report's request + artifact state — never the PDF bytes themselves."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository_id: uuid.UUID
    prompt: str
    status: ReportStatus
    error: str | None
    created_at: datetime
    has_pdf: bool
    has_pptx: bool
    # False for reports generated before section data was stored, for those whose
    # payload exceeded the size budget, and for any report that did not finish —
    # section data is written before the renderers run, so a report that failed
    # while rendering can carry a payload it must never be rebuilt from. The UI
    # must not offer a rebuild it cannot, or should not, perform.
    can_rerender: bool
    schedule_id: uuid.UUID | None
    # The plan under review (or the approved one). A user's own request, not an
    # audit artifact, so it is safe in the ordinary shape.
    plan: ReportPlan | None
    plan_rounds: int
    # The template this report wears, snapshotted at generation. ``None`` for a
    # report generated before the column existed — it renders in the default.
    template: str | None


class TemplateChoice(BaseModel):
    """One shipped template, with enough colour for a UI to draw a swatch.

    No description: ``ReportStyle`` holds no prose, and UI copy belongs in the
    frontend locale files keyed by ``key``, not in an untranslated backend map.
    """

    key: str
    name: str
    paper: str
    ink: str
    accent: str


class ReportAdminResponse(ReportResponse):
    """The admin ``?all=true`` shape: adds the audit trail — ``generated_sql``
    (legacy single-SQL reports) and ``sections`` (blended reports)."""

    generated_sql: str | None
    sections: list[dict[str, Any]] | None


class _BulkFlags(NamedTuple):
    """Whether each bulk column is populated, without reading any of them.

    Three booleans stand in for a PDF, a PPTX and up to 4 MiB of stored result
    rows. A single report is loaded whole and derives these in Python; the list
    endpoint — the one the UI polls — computes them in SQL instead (see
    ``_list_statement``), because "is it there" never justified transferring it.
    """

    has_pdf: bool
    has_pptx: bool
    has_sections_data: bool


def _base_fields(report: GeneratedReport, flags: _BulkFlags | None = None) -> dict[str, Any]:
    if flags is None:
        flags = _BulkFlags(
            has_pdf=report.pdf_data is not None,
            has_pptx=report.pptx_data is not None,
            has_sections_data=report.sections_data is not None,
        )
    return {
        "id": report.id,
        "repository_id": report.repository_id,
        "prompt": report.prompt,
        "status": report.status,
        "error": report.error,
        "created_at": report.created_at,
        "has_pdf": flags.has_pdf,
        "has_pptx": flags.has_pptx,
        "can_rerender": flags.has_sections_data and report.status is ReportStatus.DONE,
        "schedule_id": report.schedule_id,
        "plan": ReportPlan.model_validate(report.plan) if report.plan is not None else None,
        "plan_rounds": report.plan_rounds,
        "template": load_design(report.design).template,
    }


def _to_response(report: GeneratedReport, flags: _BulkFlags | None = None) -> ReportResponse:
    return ReportResponse(**_base_fields(report, flags))


def _to_admin_response(
    report: GeneratedReport, flags: _BulkFlags | None = None
) -> ReportAdminResponse:
    return ReportAdminResponse(
        **_base_fields(report, flags), generated_sql=report.generated_sql, sections=report.sections
    )


# Everything a list row renders from. The bulk columns are deliberately absent:
# with ``load_only`` they are not fetched, and nothing in the list path may touch
# them (an attribute access would be a lazy load — blocking I/O under asyncio).
_LIST_COLUMNS = (
    GeneratedReport.repository_id,
    GeneratedReport.prompt,
    GeneratedReport.status,
    GeneratedReport.error,
    GeneratedReport.created_at,
    GeneratedReport.schedule_id,
    GeneratedReport.plan,
    GeneratedReport.plan_rounds,
    GeneratedReport.design,
)


def _list_statement(*where: Any, audit: bool = False) -> Select[Any]:
    """Report rows plus the three bulk-column flags, newest first.

    ``audit=True`` adds the admin-only trail (``generated_sql``, ``sections``).
    """
    columns = (
        (*_LIST_COLUMNS, GeneratedReport.generated_sql, GeneratedReport.sections)
        if audit
        else _LIST_COLUMNS
    )
    return (
        select(
            GeneratedReport,
            GeneratedReport.pdf_data.isnot(None).label("has_pdf"),
            GeneratedReport.pptx_data.isnot(None).label("has_pptx"),
            GeneratedReport.sections_data.isnot(None).label("has_sections_data"),
        )
        .options(load_only(*columns))
        .where(*where)
        .order_by(GeneratedReport.created_at.desc())
    )


async def _get_own_report_or_404(
    session: AsyncSession, user: User, repository_id: uuid.UUID, report_id: uuid.UUID
) -> GeneratedReport:
    """Load a report scoped to its repository, owner-or-admin only.

    A non-admin reaching for another user's report gets 404 — the same as an
    unknown report id — so existence is never revealed to a stranger.
    """
    await get_repo_or_404(session, repository_id)
    report = await session.get(GeneratedReport, report_id)
    if report is None or report.repository_id != repository_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    if user.role != Role.ADMIN and report.requested_by != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report


@router.get("/report-templates")
async def list_report_templates(
    _: User = Depends(get_current_user),
) -> list[TemplateChoice]:
    """The templates a report may be set to. Default first — ``TEMPLATES`` is
    ordered, and Кварта leads it."""
    return [
        TemplateChoice(
            key=style.key,
            name=style.name,
            paper=style.paper,
            ink=style.ink,
            accent=style.accent,
        )
        for style in TEMPLATES.values()
    ]


class ReportDatabaseResponse(BaseModel):
    """One connection as a *report reader* sees it (#203).

    Deliberately not ``DatabaseConnectionResponse``: that one is the admin's
    editing view and carries host, port, username and the curated schema. A
    reader reviewing a plan needs to know which database a section will read,
    which is a name — nothing here is connection detail.
    """

    id: uuid.UUID
    name: str
    description: str


@router.get("/repositories/{repository_id}/report-databases")
async def list_report_databases(
    repository_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReportDatabaseResponse]:
    """The databases a plan for this repository can name, for the plan editor.

    Guarded like the report routes rather than admin-only (``GET
    .../databases``): the plan editor is a reader's screen, and a section routed
    to a database the reader cannot see the name of is routing they cannot
    review. Ordered by name, matching the admin list.
    """
    await guard_repo_access(session, user, repository_id)
    result = await session.execute(
        select(DatabaseConnection)
        .where(DatabaseConnection.repository_id == repository_id)
        .order_by(DatabaseConnection.name)
    )
    return [
        ReportDatabaseResponse(id=c.id, name=c.name, description=c.description)
        for c in result.scalars()
    ]


async def _connection_ids(session: AsyncSession, repository_id: uuid.UUID) -> set[str]:
    """The databases a plan for this repository may name (#203).

    Read from the repository rather than from the report's own ``connection_id``:
    an approved or re-planned draft is untrusted caller input, and this set is
    what stops a hand-crafted body routing a section at *another* repository's
    database. Ids are compared as strings because that is how a plan — a JSONB
    document — carries them.
    """
    rows = await session.execute(
        select(DatabaseConnection.id).where(DatabaseConnection.repository_id == repository_id)
    )
    return {str(row) for row in rows.scalars().all()}


@router.post("/repositories/{repository_id}/reports", status_code=status.HTTP_201_CREATED)
async def create_report(
    repository_id: uuid.UUID,
    payload: ReportRequest,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    """Request a report: creates a ``PLANNING`` row and dispatches planning as a
    background task, returning immediately for the caller to poll until the row
    reaches ``AWAITING_APPROVAL`` with a plan attached. 400 when the repository has
    neither a connected database nor any ingested sources.

    **Creation does not choose a database (#203).** It used to resolve "the
    repository's connection" with ``scalar_one_or_none()``, which #201 turned into
    a crash the moment a repository owned two — and picking one here would have
    been a guess made before the plan exists, answering a question about one
    database from another. The plan routes each section instead, and
    ``generate_report`` writes ``connection_id`` once it knows.
    """
    await guard_repo_access(session, user, repository_id)
    has_connection = (
        await session.execute(
            select(DatabaseConnection.id)
            .where(DatabaseConnection.repository_id == repository_id)
            .limit(1)
        )
    ).first() is not None
    if not has_connection and not await has_done_sources(session, repository_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connect a database or add sources to this repository first",
        )

    report = GeneratedReport(
        repository_id=repository_id,
        requested_by=user.id,
        prompt=payload.prompt,
        status=ReportStatus.PLANNING,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)

    background_tasks.add_task(run_report_planning, report.id)
    return _to_response(report)


@router.get("/repositories/{repository_id}/reports", response_model=None)
async def list_reports(
    repository_id: uuid.UUID,
    all_reports: bool = Query(default=False, alias="all"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReportResponse]:
    """The caller's own reports, newest first. ``?all=true`` is an admin-only
    escape hatch onto every user's reports for this repository, each row then
    also carrying ``generated_sql`` (the audit field)."""
    await get_repo_or_404(session, repository_id)

    if all_reports:
        if user.role != Role.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
        stmt = _list_statement(GeneratedReport.repository_id == repository_id, audit=True)
        rows = (await session.execute(stmt)).all()
        return [_to_admin_response(report, _BulkFlags(*flags)) for report, *flags in rows]

    if user.role != Role.ADMIN and not await grant_service.has_active_grant(
        session, user.id, repository_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )
    stmt = _list_statement(
        GeneratedReport.repository_id == repository_id,
        GeneratedReport.requested_by == user.id,
    )
    rows = (await session.execute(stmt)).all()
    return [_to_response(report, _BulkFlags(*flags)) for report, *flags in rows]


@router.get("/repositories/{repository_id}/reports/{report_id}")
async def get_report(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    """A single report, for polling status; owner or admin only."""
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    return _to_response(report)


async def _awaiting_or_409(
    session: AsyncSession, user: User, repository_id: uuid.UUID, report_id: uuid.UUID
) -> GeneratedReport:
    """Both plan endpoints act only on a report that is actually waiting on its owner.

    Ownership is checked first (``_get_own_report_or_404``) so a stranger gets 404,
    not 403 — a report's existence is never revealed to someone who isn't its owner
    or an admin. The grant is re-checked after: both endpoints can dispatch fresh
    work against the repository's reporting database, so a user whose grant was
    revoked or expired while their report sat at ``AWAITING_APPROVAL`` must not be
    able to use either endpoint to run it anyway.
    """
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    await guard_repo_access(session, user, repository_id)
    if report.status != ReportStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_NOT_AWAITING,
        )
    return report


async def _leave_awaiting_approval(
    session: AsyncSession, report: GeneratedReport, *, to: ReportStatus, **extra_values: Any
) -> None:
    """Atomically move a report out of ``AWAITING_APPROVAL``, or 409 if it already left.

    A conditional ``UPDATE ... WHERE status = AWAITING_APPROVAL`` — not a plain
    read-then-write — because both ``/replan`` and ``/approve`` reach this after an
    ``await`` (``finalize_draft``/``check_sources_available`` needs
    ``has_done_sources``), leaving a window where two concurrent requests on the
    same row both pass ``_awaiting_or_409``'s read and both try to act. Without
    this, a double-click on ``/approve`` could dispatch generation twice, or a
    ``/replan`` racing a winning ``/approve`` could flush ``PLANNING`` back over a
    just-committed ``PENDING`` — and since ``generate_report`` does not itself
    verify its plan was approved, that second case would let generation and
    re-planning both run against the same report. The loser here sees 0 rows
    affected and 409s instead.
    """
    result = await session.execute(
        update(GeneratedReport)
        .where(
            GeneratedReport.id == report.id,
            GeneratedReport.status == ReportStatus.AWAITING_APPROVAL,
        )
        .values(status=to, **extra_values)
    )
    if cast(CursorResult[Any], result).rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_NOT_AWAITING,
        )


@router.post("/repositories/{repository_id}/reports/{report_id}/replan")
async def replan_report_endpoint(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    payload: ReportPlanDraft,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    """Send an edited plan back for another round: the model fills only what the
    caller left blank, and every value they supplied survives verbatim.

    The submitted draft is untrusted input, so it is re-validated through the same
    availability check that guards LLM output before anything is scheduled — a
    hand-crafted section naming a source this repository lacks is rejected here
    rather than being carried into the repair loop, where the model has no way to
    fix it and every retry would be spent for nothing. The transition back to
    ``PLANNING`` is the same conditional write ``/approve`` uses (see
    ``_leave_awaiting_approval``): otherwise this could race a winning
    ``/approve`` and flush ``PLANNING`` back over a just-committed ``PENDING``.
    """
    report = await _awaiting_or_409(session, user, repository_id, report_id)
    try:
        check_sources_available(
            payload.sections,
            connection_ids=await _connection_ids(session, repository_id),
            has_documents=await has_done_sources(session, repository_id),
        )
    except ReportPlanParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await _leave_awaiting_approval(session, report, to=ReportStatus.PLANNING)
    await session.commit()
    await session.refresh(report)
    background_tasks.add_task(run_report_replanning, report.id, payload.model_dump())
    return _to_response(report)


@router.post("/repositories/{repository_id}/reports/{report_id}/approve")
async def approve_report(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    payload: ReportPlanDraft,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    """Approve a plan and start generating it.

    The body is the plan as it stands on the caller's screen, so an edit made after
    the last round is not lost. It is untrusted input and is re-validated here — a
    hand-crafted section naming a source this repository lacks is rejected the same
    way the model's own output would be.

    The transition to ``PENDING`` is a conditional write, not a plain
    read-then-write (see ``_leave_awaiting_approval``): two concurrent ``/approve``
    posts on the same row — a UI double-click is enough — would otherwise both
    observe ``AWAITING_APPROVAL`` and both dispatch generation. The loser here sees
    0 rows affected and gets 409 instead of a second pipeline running against the
    same report.
    """
    report = await _awaiting_or_409(session, user, repository_id, report_id)
    try:
        plan = finalize_draft(
            payload,
            connection_ids=await _connection_ids(session, repository_id),
            has_documents=await has_done_sources(session, repository_id),
        )
    except ReportPlanParseError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    await _leave_awaiting_approval(
        session, report, to=ReportStatus.PENDING, plan=plan.model_dump(), error=None
    )
    await session.commit()
    await session.refresh(report)
    background_tasks.add_task(run_report_generation, report.id)
    return _to_response(report)


@router.post("/repositories/{repository_id}/reports/{report_id}/rerender")
async def rerender_report_endpoint(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    """Rebuild both artifacts from stored section data.

    Synchronous on purpose: this reads storage and runs the renderers — about a
    second — and the caller wants the artifact, not a status to poll. A report
    generated before ``sections_data`` existed carries none and answers 409 rather
    than silently regenerating, which would cost a full run and produce a different
    report.
    """
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    try:
        await rerender_report(report.id)
    except ReportNotRenderable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This report has no stored section data and cannot be rebuilt.",
        ) from exc
    await session.refresh(report)
    return _to_response(report)


@router.get("/repositories/{repository_id}/reports/{report_id}/design/preview")
async def preview_report_design(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The design canvas's document: the printed report's own HTML, in preview mode.

    The same assembly ``rerender`` runs and then a stop before printing —
    **no provider call, and nothing written**. Step 5c measured the split: page
    assembly is under 6 ms against 257 ms to print on the dyno, and this path also
    skips building the deck and storing two artifacts.

    Refuses exactly as ``rerender`` refuses, because it reads exactly what
    ``rerender`` reads: 404 for a report outside the caller's repository, 409 for
    one with no stored section data or one that never finished.
    """
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    try:
        html = await preview_report_html(report.id)
    except ReportNotRenderable as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This report has no stored section data and cannot be rebuilt.",
        ) from exc
    # No caching header: the document changes on every design edit, and the
    # editor asks for it after each one.
    return Response(content=html, media_type="text/html; charset=utf-8")


@router.get("/repositories/{repository_id}/reports/{report_id}/design")
async def get_report_design(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportDesignResponse:
    """The block list this report renders from, and what each section permits.

    Its own endpoint rather than fields on ``ReportResponse``, for a reason the
    list path already encodes: ``_list_statement`` uses ``load_only`` precisely so
    ``sections_data`` — up to 4 MiB — is never fetched for a row the UI is merely
    listing. Answering "which chart types does section 3 allow?" needs the full
    result rows *and* a ``prepare_plot`` probe per candidate, so putting it on the
    report shape would make every poll of the gallery pay for an editor nobody
    has opened.

    A report with no stored section data answers with empty lists rather than an
    error. ``can_rerender`` is already how a client learns that report cannot be
    rebuilt; a second, differently-shaped refusal here would only be a second
    thing to handle, and a GET that reads a legitimately existing row should not
    fail.
    """
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    template = load_design(report.design).template
    if not report.sections_data:
        return ReportDesignResponse(
            template=template,
            title=None,
            blocks_version=BLOCKS_VERSION,
            blocks=[],
            stored=False,
            sections=[],
            typography=_group(report.design, "typography"),
            page=_group(report.design, "page"),
            brand=_group(report.design, "brand"),
        )

    title, _, sections = load_sections(report.sections_data)
    stored_blocks = load_blocks(report.design)
    return ReportDesignResponse(
        template=template,
        title=title,
        blocks_version=BLOCKS_VERSION,
        # ``default_blocks_from_storage``, never ``default_blocks``: these sections
        # came back from storage, so their ``chart_svg`` is None by construction
        # and the plain derivation would show a tree with no figure in it beside a
        # sheet that plainly has one.
        blocks=(
            stored_blocks if stored_blocks is not None else default_blocks_from_storage(sections)
        ),
        stored=stored_blocks is not None,
        sections=[
            ReportSectionSummary(
                index=index,
                title=section.title,
                kind=section.kind,
                allowed_chart_types=sorted(allowed_chart_types(section)),
                draws_chart=draws_chart(section),
                prose=section.prose,
                sources=(
                    " · ".join(f"[{ref.number}] {ref.title}" for ref in section.sources)
                    if section.sources
                    else None
                ),
            )
            for index, section in enumerate(sections)
        ],
        typography=_group(report.design, "typography"),
        page=_group(report.design, "page"),
        brand=_group(report.design, "brand"),
    )


async def _ensure_own_logo(session: AsyncSession, repository_id: uuid.UUID, raw_logo: Any) -> None:
    """Refuse a cover logo that belongs to another repository.

    The same rule ``validate_blocks`` applies to an ``image`` block, and for the
    same reason: this endpoint takes a payload the client composes, so a picker
    that only lists your own assets restricts nothing. Without it a crafted
    ``PUT`` prints another repository's bytes on this report's cover.
    """
    if raw_logo is None:
        return
    try:
        logo_id = uuid.UUID(str(raw_logo))
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="brand.logo_asset_id must be an asset id.",
        ) from exc
    owned = await session.scalar(
        select(ReportAsset.id).where(
            ReportAsset.id == logo_id, ReportAsset.repository_id == repository_id
        )
    )
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Asset {logo_id} does not belong to this repository.",
        )


@router.put("/repositories/{repository_id}/reports/{report_id}/design")
async def set_report_design(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    payload: ReportDesignRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportResponse:
    """Store the design this report should wear. Does **not** rebuild it.

    Storing and rebuilding stay two operations: it is what keeps ``rerender``
    meaning exactly "rebuild from storage", and what step 4's editor needs, since
    an editor saves on every change and must not rebuild a PDF each time. Between
    this call and ``rerender`` the row and the stored artifacts disagree — no
    staleness flag exists yet, and none is invented for a UI that does not exist.

    Allowed in any status, including a report with no stored section data. The
    design is an intent; ``rerender`` still refuses on its own terms with a 409.

    A *partial* update: omitting ``template`` or ``blocks`` leaves that part of
    the stored design alone, and sending either as ``null`` clears it. ``blocks``
    must arrive with the ``blocks_version`` it was built against — never assumed
    — and a report with no stored section data cannot store a block list at all,
    since it would reference sections that do not exist.
    """
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    stored = dict(report.design or {})

    if "template" in payload.model_fields_set:
        ensure_known_template(payload.template)
        if payload.template is None:
            stored.pop("template", None)
        else:
            stored.update(design_payload(payload.template))

    if "blocks" in payload.model_fields_set:
        if payload.blocks is None:
            stored.pop("blocks", None)
            stored.pop("blocks_version", None)
        else:
            if payload.blocks_version != BLOCKS_VERSION:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        f"blocks require blocks_version {BLOCKS_VERSION}. "
                        "A payload whose version was inferred is exactly the ambiguity "
                        "the field exists to remove."
                    ),
                )
            if not report.sections_data:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=(
                        "This report has no stored section data, so a block list "
                        "referencing its sections cannot be stored."
                    ),
                )
            _, _, sections = load_sections(report.sections_data)
            owned_assets = set(
                await session.scalars(
                    select(ReportAsset.id).where(ReportAsset.repository_id == repository_id)
                )
            )
            try:
                validate_blocks(payload.blocks, sections, asset_ids=owned_assets)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
                ) from exc
            # ``exclude_defaults`` rather than ``exclude_none``: identical for every
            # field that defaults to None, and it additionally keeps ``hidden:
            # false`` out of the payload. The stored list records departures from
            # the default, so adding a flag does not rewrite every block already
            # written — and a JSON diff of two designs stays about what changed.
            stored["blocks"] = [block.model_dump(exclude_defaults=True) for block in payload.blocks]
            stored["blocks_version"] = BLOCKS_VERSION

    for group in ("typography", "page", "brand"):
        if group not in payload.model_fields_set:
            continue
        value = getattr(payload, group)
        if value is None:
            stored.pop(group, None)
            continue
        if group == "brand":
            await _ensure_own_logo(session, repository_id, value.get("logo_asset_id"))
        stored[group] = value

    # A JSONB column needs a fresh object for SQLAlchemy to notice the change.
    report.design = stored or None
    await session.commit()
    return _to_response(report)


_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@router.get("/repositories/{repository_id}/reports/{report_id}/download")
async def download_report(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    format: Literal["pdf", "pptx"] = "pdf",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download a report in the requested format; 404 unless generation finished
    (``DONE`` with that artifact stored). Owner or admin only.

    ``format`` defaults to ``pdf`` so existing links keep working. Reports
    generated before PPTX existed have no ``pptx_data`` and 404 for that format
    rather than silently returning a PDF under a .pptx name.
    """
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    data = report.pdf_data if format == "pdf" else report.pptx_data
    filename = report.pdf_filename if format == "pdf" else report.pptx_filename
    if report.status != ReportStatus.DONE or data is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report {format.upper()} not available",
        )
    return Response(
        content=data,
        media_type=_MEDIA_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/repositories/{repository_id}/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_report(
    repository_id: uuid.UUID,
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a report; owner or admin only."""
    report = await _get_own_report_or_404(session, user, repository_id, report_id)
    await session.delete(report)
    await session.commit()
