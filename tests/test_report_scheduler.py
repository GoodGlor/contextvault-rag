"""Nightly scheduler: due-schedule selection and frozen-SQL re-execution.

DB-backed: uses the ``db_session`` fixture and skips when no migrated database
is reachable (see conftest). The ``_connected_repo`` helper mirrors the one in
``tests/test_reports_service.py`` (Task 8) — re-created locally per the task
brief rather than imported across test files.
"""

import datetime
import zoneinfo
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.core.crypto import encrypt
from contextvault.models import (
    DatabaseConnection,
    DatabaseType,
    GeneratedReport,
    ReportSchedule,
    ReportStatus,
    Repository,
)
from contextvault.services import reports as reports_service
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_scheduler import find_due_schedules, run_due_schedules


def _fixed_factory(session: AsyncSession):  # type: ignore[no-untyped-def]
    """A session factory that always yields ``session`` without closing it."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory


async def _connected_repo(db_session: AsyncSession) -> tuple[Repository, DatabaseConnection]:
    url = make_url(get_settings().database_url)
    repo = Repository(name="Vault", llm_provider="gemini", llm_model="gem-1")
    db_session.add(repo)
    await db_session.flush()
    conn = DatabaseConnection(
        repository_id=repo.id,
        name="Reporting",
        db_type=DatabaseType.POSTGRES,
        host=url.host or "localhost",
        port=url.port or 5432,
        database=url.database or "",
        username=url.username or "",
        password_encrypted=encrypt(url.password or ""),
        exposed_schema=[
            {
                "table": "repositories",
                "description": "",
                "columns": [{"name": "name", "description": ""}],
            }
        ],
    )
    db_session.add(conn)
    await db_session.flush()
    return repo, conn


def _schedule(
    repo: Repository,
    conn: DatabaseConnection,
    *,
    frozen_sql: str,
    run_at_time: datetime.time,
    enabled: bool = True,
    last_run_at: datetime.datetime | None = None,
) -> ReportSchedule:
    return ReportSchedule(
        repository_id=repo.id,
        connection_id=conn.id,
        owner_id=None,
        prompt="names",
        frozen_sql=frozen_sql,
        frozen_chart_spec=ChartSpec(chart_type="none", title="t").model_dump(),
        run_at_time=run_at_time,
        enabled=enabled,
        last_run_at=last_run_at,
    )


async def test_due_logic(db_session: AsyncSession) -> None:
    """The five selection cases, stated in the reader's clock.

    **This test read in UTC until #164**, because that is what the code did:
    `now` was 01:05 UTC and the schedules sat at 01:00 and 02:00 as if those were
    UTC times too. They never were — `run_at_time` is what somebody typed into a
    form. The instants below are chosen so the local clock reads 01:05 on the
    23rd, which is what the old numbers were *meant* to say; every one of the
    five cases is unchanged.
    """
    repo, conn = await _connected_repo(db_session)
    # 22:05 UTC on the 22nd is 01:05 in Kyiv on the 23rd.
    now = datetime.datetime(2026, 7, 22, 22, 5, tzinfo=datetime.UTC)

    due_no_prior_run = _schedule(
        repo, conn, frozen_sql="SELECT name FROM repositories", run_at_time=datetime.time(1, 0)
    )
    due_ran_yesterday = _schedule(
        repo,
        conn,
        frozen_sql="SELECT name FROM repositories",
        run_at_time=datetime.time(1, 0),
        # 01:05 Kyiv on the 22nd — yesterday, locally.
        last_run_at=datetime.datetime(2026, 7, 21, 22, 5, tzinfo=datetime.UTC),
    )
    already_ran_today = _schedule(
        repo,
        conn,
        frozen_sql="SELECT name FROM repositories",
        run_at_time=datetime.time(1, 0),
        # 01:01 Kyiv on the 23rd — four minutes ago, locally.
        last_run_at=datetime.datetime(2026, 7, 22, 22, 1, tzinfo=datetime.UTC),
    )
    not_yet_time = _schedule(
        repo, conn, frozen_sql="SELECT name FROM repositories", run_at_time=datetime.time(2, 0)
    )
    disabled = _schedule(
        repo,
        conn,
        frozen_sql="SELECT name FROM repositories",
        run_at_time=datetime.time(1, 0),
        enabled=False,
    )
    for s in (due_no_prior_run, due_ran_yesterday, already_ran_today, not_yet_time, disabled):
        db_session.add(s)
    await db_session.flush()

    due = await find_due_schedules(db_session, now=now)
    due_ids = {s.id for s in due}

    assert due_no_prior_run.id in due_ids
    assert due_ran_yesterday.id in due_ids
    assert already_ran_today.id not in due_ids
    assert not_yet_time.id not in due_ids
    assert disabled.id not in due_ids


async def test_run_due_schedules_executes_frozen_and_stamps(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, conn = await _connected_repo(db_session)
    schedule = _schedule(
        repo, conn, frozen_sql="SELECT name FROM repositories", run_at_time=datetime.time(1, 0)
    )

    # The SQL runs for real over its own connection, so the repository inserted
    # through `db_session` is invisible to it and the query returns whatever
    # earlier runs left committed — nothing, on a clean database. That was
    # harmless while an empty result still produced a report; since #185 the run
    # is refused and this test's subject (the schedule is stamped and stays
    # enabled) would be decided by how dirty the database was.
    async def matching_rows(**kwargs: object) -> QueryResult:
        return QueryResult(columns=["name"], rows=[("Бухгалтерія",)])

    monkeypatch.setattr(reports_service, "run_validated_query", matching_rows)
    db_session.add(schedule)
    await db_session.flush()
    schedule_id = schedule.id

    now = datetime.datetime(2026, 7, 23, 1, 5, tzinfo=datetime.UTC)
    await run_due_schedules(now=now, session_factory=_fixed_factory(db_session))

    await db_session.refresh(schedule)
    assert schedule.last_run_at == now
    assert schedule.last_error is None
    assert schedule.enabled is True

    result = await db_session.execute(
        sa.select(GeneratedReport).where(GeneratedReport.schedule_id == schedule_id)
    )
    report = result.scalar_one()
    assert report.status is ReportStatus.DONE
    assert report.requested_by == schedule.owner_id


async def test_failed_run_records_last_error_but_keeps_schedule_enabled(
    db_session: AsyncSession,
) -> None:
    repo, conn = await _connected_repo(db_session)
    schedule = _schedule(
        repo, conn, frozen_sql="SELECT nope FROM missing", run_at_time=datetime.time(1, 0)
    )
    db_session.add(schedule)
    await db_session.flush()
    schedule_id = schedule.id

    now = datetime.datetime(2026, 7, 23, 1, 5, tzinfo=datetime.UTC)
    await run_due_schedules(now=now, session_factory=_fixed_factory(db_session))

    await db_session.refresh(schedule)
    assert schedule.last_run_at == now
    assert schedule.last_error is not None
    assert schedule.enabled is True

    result = await db_session.execute(
        sa.select(GeneratedReport).where(GeneratedReport.schedule_id == schedule_id)
    )
    report = result.scalar_one()
    assert report.status is ReportStatus.FAILED


# --- the hour a schedule actually fires (#164) --------------------------------
#
# `run_at_time` is a naive wall-clock time the user typed. Comparing it against
# `datetime.now(UTC).time()` treated Kyiv's 02:00 as UTC's, so a report asked for
# at two in the morning arrived at five. These assert against a *named zone* and
# in both halves of the year, so a fixed +2 or +3 offset cannot pass either.

_KYIV = zoneinfo.ZoneInfo("Europe/Kyiv")


async def test_a_two_am_schedule_is_not_due_at_two_am_utc(db_session: AsyncSession) -> None:
    """The bug, stated as a test. At 02:00 UTC it is 05:00 in Kyiv — but a reader
    who asked for 02:00 meant their own two o'clock, which was three hours ago
    and is *yesterday's* run as far as this instant is concerned."""
    repo, conn = await _connected_repo(db_session)
    # 2026-07-23 01:00 UTC is 04:00 Kyiv, so 02:00 Kyiv has passed and the run is
    # due. The instant that must NOT trigger it is one where Kyiv is before 02:00.
    before_two_kyiv = datetime.datetime(2026, 7, 22, 22, 30, tzinfo=datetime.UTC)  # 01:30 Kyiv
    schedule = _schedule(repo, conn, frozen_sql="SELECT 1", run_at_time=datetime.time(2, 0))
    db_session.add(schedule)
    await db_session.flush()

    due = await find_due_schedules(db_session, now=before_two_kyiv)

    assert schedule.id not in {s.id for s in due}


async def test_a_two_am_schedule_is_due_just_after_two_am_local(db_session: AsyncSession) -> None:
    repo, conn = await _connected_repo(db_session)
    just_after = datetime.datetime(2026, 7, 22, 23, 5, tzinfo=datetime.UTC)  # 02:05 Kyiv, 23rd
    schedule = _schedule(repo, conn, frozen_sql="SELECT 1", run_at_time=datetime.time(2, 0))
    db_session.add(schedule)
    await db_session.flush()

    due = await find_due_schedules(db_session, now=just_after)

    assert schedule.id in {s.id for s in due}


async def test_the_same_holds_in_winter_when_the_offset_is_different(
    db_session: AsyncSession,
) -> None:
    """Kyiv is UTC+3 in July and UTC+2 in January. A fix that added a constant
    would pass the summer tests and fail here — which is the point of having it."""
    repo, conn = await _connected_repo(db_session)
    schedule = _schedule(repo, conn, frozen_sql="SELECT 1", run_at_time=datetime.time(2, 0))
    db_session.add(schedule)
    await db_session.flush()

    before = datetime.datetime(2026, 1, 22, 23, 30, tzinfo=datetime.UTC)  # 01:30 Kyiv, 23rd
    after = datetime.datetime(2026, 1, 23, 0, 30, tzinfo=datetime.UTC)  # 02:30 Kyiv, 23rd

    assert schedule.id not in {s.id for s in await find_due_schedules(db_session, now=before)}
    assert schedule.id in {s.id for s in await find_due_schedules(db_session, now=after)}


async def test_yesterdays_run_does_not_block_todays_across_a_local_midnight(
    db_session: AsyncSession,
) -> None:
    """The quieter half of the same bug.

    The guard asked whether ``last_run_at``'s **UTC** date was earlier than
    today's UTC date. Between local midnight and 03:00 the two disagree: it is
    already the 23rd in Kyiv while UTC still says the 22nd, so a run recorded
    yesterday-local reads as "already ran today" and the schedule is skipped.
    """
    repo, conn = await _connected_repo(db_session)
    schedule = _schedule(
        repo,
        conn,
        frozen_sql="SELECT 1",
        run_at_time=datetime.time(2, 0),
        # 02:05 Kyiv on the 22nd — yesterday's run, in local terms.
        last_run_at=datetime.datetime(2026, 7, 21, 23, 5, tzinfo=datetime.UTC),
    )
    db_session.add(schedule)
    await db_session.flush()

    # 02:30 Kyiv on the 23rd. Same UTC date (the 22nd) as the recorded run.
    now = datetime.datetime(2026, 7, 22, 23, 30, tzinfo=datetime.UTC)
    due = await find_due_schedules(db_session, now=now)

    assert schedule.id in {s.id for s in due}


async def test_a_run_earlier_today_local_still_blocks_a_second_one(
    db_session: AsyncSession,
) -> None:
    """The guard must still guard. Once a day, not once a poll."""
    repo, conn = await _connected_repo(db_session)
    schedule = _schedule(
        repo,
        conn,
        frozen_sql="SELECT 1",
        run_at_time=datetime.time(2, 0),
        last_run_at=datetime.datetime(2026, 7, 22, 23, 6, tzinfo=datetime.UTC),  # 02:06 Kyiv, 23rd
    )
    db_session.add(schedule)
    await db_session.flush()

    now = datetime.datetime(2026, 7, 23, 6, 0, tzinfo=datetime.UTC)  # 09:00 Kyiv, same local day
    due = await find_due_schedules(db_session, now=now)

    assert schedule.id not in {s.id for s in due}


def test_the_configured_zone_is_a_real_one() -> None:
    """A typo here is a scheduler that quietly never fires at the right hour, so
    it is a boot failure instead. Asserted rather than assumed: the default ships
    as the deployment's own zone."""
    assert zoneinfo.ZoneInfo(get_settings().schedule_timezone) == _KYIV
