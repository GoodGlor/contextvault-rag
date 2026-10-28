"""What the schema block cannot say: how long a fact's period is.

A long-format fact table stores quarterly, year-to-date and annual figures for
one metric under the same fiscal-period label, distinguished only by
``period_end - period_start``. Nothing in a column name reveals that, and a
model that cannot see it writes a query summing overlapping rows — the measured
cause of a report claiming Apple earned $1.2 trillion.

The notes are **measured, never authored.** Prose in a file cannot track a
column that gets renamed, and the authored notes in ``tools/`` have already
drifted against this same database.
"""

import datetime
import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

from contextvault.models import DatabaseType
from contextvault.services.report_execution import run_validated_query
from contextvault.services.sql_guardrails import DIALECTS

logger = logging.getLogger(__name__)

# Below this many rows a bucket is noise rather than a shape worth naming.
_MIN_BUCKET_ROWS = 20
# Buckets closer than this in length are the same kind of period — a 90-day and
# a 97-day quarter are both quarters.
_MEANINGFUL_RATIO = 2.0
_MAX_BUCKETS = 5
# A probe is a nicety; it must never be what makes a report slow. `GROUP BY` on
# an unindexed expression is a full scan, and no LIMIT rescues it because
# Postgres's HashAggregate is blocking.
_PROBE_TIMEOUT_MS = 2_000

# This module is the one place that puts an allow-listed name into SQL without
# `validate_sql` seeing it. The entries are admin-curated from introspection, so
# this is belt-and-braces rather than a live threat — but a quoted identifier
# ends at a quote, and nothing else here would notice.
_PLAIN_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# The quote character matters: in MySQL a double-quoted name is a string
# literal, not an identifier, so one shared template would make the MySQL branch
# fail into `except` forever while looking implemented.
_DURATION_SQL = {
    DatabaseType.POSTGRES: ('("{end}" - "{start}")', '"'),
    DatabaseType.MYSQL: ("DATEDIFF(`{end}`, `{start}`)", "`"),
}


def _as_days(value: Any) -> int | None:
    """A duration as a whole number of days, or None if it is not one.

    Postgres subtracts two ``date`` columns to an integer but two ``timestamp``
    columns to an interval, and MySQL's ``DATEDIFF`` returns a Decimal. Anything
    that is not a whole number of days is dropped rather than guessed at: this
    runs outside the probe's ``except``, so a TypeError here would fail a report
    over a hint.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return value.days if value.seconds == 0 and value.microseconds == 0 else None
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def period_pairs(exposed_schema: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Every allow-listed table with a ``*_start`` / ``*_end`` column pair."""
    found: list[tuple[str, str, str]] = []
    for table in exposed_schema:
        name = table.get("table", "")
        if not _PLAIN_IDENTIFIER.match(name):
            continue
        columns = [column["name"] for column in table.get("columns", [])]
        for column in columns:
            if not column.endswith("_start") or not _PLAIN_IDENTIFIER.match(column):
                continue
            end = column[: -len("_start")] + "_end"
            if end in columns:
                found.append((name, column, end))
    return found


def duration_note(buckets: list[tuple[int | None, int]]) -> str | None:
    """A one-line note about the period lengths present, or None for silence."""
    kept = [(days, rows) for days, rows in buckets if rows >= _MIN_BUCKET_ROWS]
    spans = sorted({days for days, _ in kept if days is not None and days > 0})
    instants = sum(rows for days, rows in kept if days is None)
    if len(spans) < 2 or max(spans) / min(spans) < _MEANINGFUL_RATIO:
        return None
    shown = sorted(
        ((days, rows) for days, rows in kept if days is not None),
        key=lambda item: item[1],
        reverse=True,
    )[:_MAX_BUCKETS]
    listed = ", ".join(f"{days}d ({rows} rows)" for days, rows in shown)
    note = (
        f"period lengths present: {listed}. Quarterly, year-to-date and annual facts "
        f"can share one period label and overlap each other; constrain "
        f"(period_end - period_start) to select one kind"
    )
    if instants:
        note += f". {instants} rows are instant facts with no period at all"
    return note


def mixed_duration_tables(period_notes: dict[str, str]) -> set[str]:
    """The allow-listed tables whose facts are not all the same length.

    Free, and deliberately derived rather than measured again: ``duration_note``
    returns ``None`` unless a table holds at least two meaningfully different
    period lengths, so ``probe_period_notes`` only ever has a key for a table
    that is mixed. The keys *are* the answer.
    """
    return set(period_notes)


def aggregates_without_duration(
    sql: str,
    *,
    db_type: DatabaseType,
    mixed_tables: set[str],
    exposed_schema: list[dict[str, Any]],
) -> str | None:
    """The mixed-duration table this query aggregates without constraining, if any.

    #176 tests the *result rows* and is right about them, but a query that says
    ``SUM(value) … GROUP BY fiscal_year`` has already added the quarterly, the
    year-to-date and the annual fact together inside the database. One row per
    year comes back, the keys are genuinely distinct, and the figure is three
    times too large. Nothing downstream can see that, so it is caught here —
    before execution, from the SQL alone.

    **Lenient by decision.** Any reference to either duration column, anywhere in
    the statement, exempts the query: if the model mentioned the period at all,
    assume it was thinking about periods. A false positive spends a retry and
    puts a warning on a correct page, which is the more expensive mistake.

    Columns are matched by name, not by resolved table, so a join across two
    mixed tables where only one is constrained will not fire. Accepted.
    """
    if not mixed_tables:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=DIALECTS[db_type])
    except sqlglot.errors.ParseError:
        # `validate_sql` owns parse failures and runs before this; a hint must
        # never be the thing that fails a report.
        return None
    if tree is None:
        return None
    if tree.find(exp.AggFunc) is None and tree.find(exp.Group) is None:
        return None
    named = {table.name.lower() for table in tree.find_all(exp.Table)}
    referenced = {column.name.lower() for column in tree.find_all(exp.Column)}
    lowered = {table.lower() for table in mixed_tables}
    for table, start, end in period_pairs(exposed_schema):
        if table.lower() not in lowered or table.lower() not in named:
            continue
        if start.lower() in referenced or end.lower() in referenced:
            continue
        return table
    return None


async def probe_period_notes(
    *,
    conn_params: dict[str, Any],
    db_type: DatabaseType,
    exposed_schema: list[dict[str, Any]],
) -> dict[str, str]:
    """``{table: note}``, measured once per report. Never raises.

    A hint is an optimisation, never a dependency: a probe that times out, errors
    or meets a stale allow-list entry simply produces no note, and the schema
    block looks exactly as it did before.

    ``conn_params`` is the flat credential dict ``reports._conn_params`` builds,
    passed in rather than derived here — importing it would be circular, because
    ``reports`` imports this module.
    """
    dialect = _DURATION_SQL.get(db_type)
    if dialect is None:
        return {}
    template, quote = dialect
    notes: dict[str, str] = {}
    for table, start, end in period_pairs(exposed_schema):
        expression = template.format(start=start, end=end)
        sql = (
            f"SELECT {expression} AS days, COUNT(*) AS n "
            f"FROM {quote}{table}{quote} GROUP BY 1 ORDER BY n DESC LIMIT 12"
        )
        try:
            result = await run_validated_query(**conn_params, sql=sql, timeout_ms=_PROBE_TIMEOUT_MS)
            buckets = [(_as_days(row[0]), int(row[1])) for row in result.rows]
        except Exception:  # noqa: BLE001 - best-effort by design
            logger.info("period probe failed for %s; continuing without a note", table)
            continue
        note = duration_note(buckets)
        if note is not None:
            notes[table] = note
    return notes
