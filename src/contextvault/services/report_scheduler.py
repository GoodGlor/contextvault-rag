"""Nightly report scheduler (Database-Backed Reports).

``find_due_schedules`` selects enabled schedules whose ``run_at_time`` has
passed and that have not already run today. ``run_due_schedules`` is the
awaitable core: it re-executes each due schedule's frozen SQL via
``execute_frozen`` (no LLM call — see ``services/reports.py``) and stamps the
schedule with when it ran and whether it failed. A failed run is recorded but
never disables the schedule — it simply tries again the following day.

``scheduler_loop`` is the process-lifetime seam: a simple poll loop, wired to
the app via ``main.py``'s lifespan. Any per-tick failure is logged and
swallowed so one bad tick can never kill the loop.
"""

import asyncio
import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.db.session import SessionLocal
from contextvault.models import ReportSchedule
from contextvault.services.ingestion import SessionFactory
from contextvault.services.reports import execute_frozen

logger = logging.getLogger(__name__)


async def find_due_schedules(
    session: AsyncSession, *, now: datetime, timezone: ZoneInfo | None = None
) -> list[ReportSchedule]:
    """Enabled schedules whose run time has passed and haven't run today.

    **"Has passed" and "today" are both local questions, and until #164 both
    were asked of UTC.** ``run_at_time`` is a naive wall-clock time a reader
    typed meaning their own two o'clock; comparing it against ``now.time()`` in
    UTC delivered a Ukrainian schedule at 04:00 or 05:00 depending on the season.

    The day boundary was wrong for the same reason and less visibly. The guard
    asked whether ``last_run_at``'s UTC *date* was earlier than today's UTC date,
    and between local midnight and 03:00 those disagree — it is already the 23rd
    in Kyiv while UTC still says the 22nd — so yesterday-local's run read as
    "already ran today" and the schedule was skipped. It now compares against
    local midnight as an **absolute instant**: ``last_run_at`` is ``timestamptz``,
    so that is an exact comparison and does not depend on the Postgres session's
    own ``TimeZone``, which nothing here sets.

    ``timezone`` is injectable so a test can pin a zone rather than inherit the
    deployment's; the default is the setting, validated at startup.
    """
    zone = timezone or ZoneInfo(get_settings().schedule_timezone)
    local = now.astimezone(zone)
    # `time.min` and not a DST-safe arithmetic dance: no zone this setting is
    # meant for shifts at midnight — Kyiv moves at 03:00/04:00 — and a zone that
    # did would want a per-schedule answer, not a cleverer expression here.
    midnight = datetime.combine(local.date(), time.min, tzinfo=zone)
    result = await session.execute(
        sa.select(ReportSchedule).where(
            ReportSchedule.enabled.is_(True),
            ReportSchedule.run_at_time <= local.time(),
            sa.or_(
                ReportSchedule.last_run_at.is_(None),
                ReportSchedule.last_run_at < midnight,
            ),
        )
    )
    return list(result.scalars().all())


async def run_due_schedules(
    *, now: datetime, session_factory: SessionFactory = SessionLocal
) -> None:
    """Re-run every due schedule's frozen query, stamping the outcome."""
    async with session_factory() as session:
        due = await find_due_schedules(session, now=now)
        for schedule in due:
            report = await execute_frozen(session, schedule=schedule)
            schedule.last_run_at = now
            schedule.last_error = report.error
            await session.commit()


async def scheduler_loop(
    *, interval_seconds: int = 60, session_factory: SessionFactory = SessionLocal
) -> None:
    """Poll forever, running due schedules once per tick. Never dies on error."""
    while True:
        try:
            await run_due_schedules(now=datetime.now(UTC), session_factory=session_factory)
        except Exception:  # noqa: BLE001 — a bad tick must not kill the loop
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(interval_seconds)
