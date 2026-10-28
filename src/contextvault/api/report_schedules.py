"""Report-schedules API: freeze a DONE report into a
nightly re-run, list, toggle, delete.

A schedule *freezes* an already-generated report's validated artifacts —
``frozen_sql``/``frozen_chart_spec`` — so the nightly scheduler
(``services/report_scheduler.py``) can re-execute them verbatim with no further
LLM call. Freezing requires the source report to: exist in the target repository,
have finished (``DONE``), belong to the caller (admins may freeze anyone's
report), and carry both ``generated_sql`` and ``chart_spec`` — anything short of
that is a 400, since the schedule would otherwise have nothing valid to freeze.

Access mirrors ``api/reports.py``: the repo-scoped create/list routes use the
query-endpoint guard (repo 404, then non-admin needs an active grant, 403
otherwise; admins bypass). The single-schedule PATCH/DELETE routes are flat
(``/report-schedules/{id}``, no repository in the path) and gate on owner-or-admin
via a shared helper — a non-admin reaching for someone else's schedule gets 404,
never 403, so the schedule's existence is never revealed to a stranger.
"""

import uuid
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import get_current_user
from contextvault.db.session import get_session
from contextvault.models import (
    GeneratedReport,
    ReportSchedule,
    ReportStatus,
    Repository,
    Role,
    User,
)
from contextvault.services import grants as grant_service

router = APIRouter(tags=["report-schedules"])


class ScheduleCreateRequest(BaseModel):
    """Freeze a source report (must be ``DONE``, owned by the caller) into a
    nightly schedule run at ``run_at_time`` (``"HH:MM"``, parsed to a bare time)."""

    report_id: uuid.UUID
    run_at_time: time


class ScheduleUpdateRequest(BaseModel):
    """Partial update; only fields present in the request body are applied."""

    enabled: bool | None = None
    run_at_time: time | None = None


class ScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repository_id: uuid.UUID
    prompt: str
    run_at_time: time
    enabled: bool
    last_run_at: datetime | None
    last_error: str | None
    created_at: datetime


def _to_response(schedule: ReportSchedule) -> ScheduleResponse:
    return ScheduleResponse.model_validate(schedule)


async def _get_repo_or_404(session: AsyncSession, repository_id: uuid.UUID) -> Repository:
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    return repo


async def _guard_repo_access(session: AsyncSession, user: User, repository_id: uuid.UUID) -> None:
    """The query-endpoint guard: repo must exist (404); non-admins need an active
    grant (403). Admins bypass the grant check."""
    await _get_repo_or_404(session, repository_id)
    if user.role != Role.ADMIN and not await grant_service.has_active_grant(
        session, user.id, repository_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )


async def _get_owned_schedule_or_404(
    session: AsyncSession, user: User, schedule_id: uuid.UUID
) -> ReportSchedule:
    """Load a schedule, owner-or-admin only.

    A non-admin reaching for another user's schedule gets 404 — the same as an
    unknown schedule id — so existence is never revealed to a stranger.
    """
    schedule = await session.get(ReportSchedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    if user.role != Role.ADMIN and schedule.owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule


@router.post("/repositories/{repository_id}/report-schedules", status_code=status.HTTP_201_CREATED)
async def create_schedule(
    repository_id: uuid.UUID,
    payload: ScheduleCreateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ScheduleResponse:
    """Freeze a ``DONE`` report belonging to this repository into a nightly
    schedule. 400 unless the report exists in this repo, is ``DONE``, belongs to
    the caller (or the caller is admin), and has both ``generated_sql`` and
    ``chart_spec`` set."""
    await _guard_repo_access(session, user, repository_id)

    report = await session.get(GeneratedReport, payload.report_id)
    if report is None or report.repository_id != repository_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Report not found in this repository",
        )
    if user.role != Role.ADMIN and report.requested_by != user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Report does not belong to the caller",
        )
    if report.status != ReportStatus.DONE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Report has not finished generating"
        )
    if report.generated_sql is None or report.chart_spec is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Report is missing generated SQL or a chart spec",
        )

    schedule = ReportSchedule(
        repository_id=repository_id,
        connection_id=report.connection_id,
        owner_id=user.id,
        prompt=report.prompt,
        frozen_sql=report.generated_sql,
        frozen_chart_spec=report.chart_spec,
        run_at_time=payload.run_at_time,
        enabled=True,
    )
    session.add(schedule)
    await session.commit()
    await session.refresh(schedule)
    return _to_response(schedule)


@router.get("/repositories/{repository_id}/report-schedules", response_model=None)
async def list_schedules(
    repository_id: uuid.UUID,
    all_schedules: bool = Query(default=False, alias="all"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ScheduleResponse]:
    """The caller's own schedules, newest first. ``?all=true`` is an admin-only
    escape hatch onto every user's schedules for this repository."""
    await _get_repo_or_404(session, repository_id)

    if all_schedules:
        if user.role != Role.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
        stmt = (
            select(ReportSchedule)
            .where(ReportSchedule.repository_id == repository_id)
            .order_by(ReportSchedule.created_at.desc())
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_response(s) for s in rows]

    if user.role != Role.ADMIN and not await grant_service.has_active_grant(
        session, user.id, repository_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )
    stmt = (
        select(ReportSchedule)
        .where(
            ReportSchedule.repository_id == repository_id,
            ReportSchedule.owner_id == user.id,
        )
        .order_by(ReportSchedule.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_to_response(s) for s in rows]


@router.patch("/report-schedules/{schedule_id}")
async def update_schedule(
    schedule_id: uuid.UUID,
    payload: ScheduleUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ScheduleResponse:
    """Toggle ``enabled`` and/or change ``run_at_time``; owner or admin only.
    Only fields present in the request body are applied."""
    schedule = await _get_owned_schedule_or_404(session, user, schedule_id)
    data = payload.model_dump(exclude_unset=True)
    if "enabled" in data:
        schedule.enabled = data["enabled"]
    if "run_at_time" in data:
        schedule.run_at_time = data["run_at_time"]
    await session.commit()
    await session.refresh(schedule)
    return _to_response(schedule)


@router.delete("/report-schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a schedule; owner or admin only."""
    schedule = await _get_owned_schedule_or_404(session, user, schedule_id)
    await session.delete(schedule)
    await session.commit()
