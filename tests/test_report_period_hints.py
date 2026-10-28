"""What the schema block cannot say: how long a fact's period is.

The note is *measured*, so these tests assert the shape of a measurement rather
than any authored prose — the authored notes in ``tools/`` drifted against this
same database, which is why the probe exists at all.
"""

import datetime
from typing import Any
from unittest import mock

import pytest

import contextvault.services.report_period_hints as report_period_hints
from contextvault.models import DatabaseType
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_period_hints import (
    _as_days,
    aggregates_without_duration,
    duration_note,
    mixed_duration_tables,
    period_pairs,
)


def test_a_start_end_pair_is_found_by_name() -> None:
    schema = [
        {
            "table": "apple_financials",
            "columns": [{"name": "tag"}, {"name": "period_start"}, {"name": "period_end"}],
        },
        {"table": "filings", "columns": [{"name": "accession"}]},
    ]
    assert period_pairs(schema) == [("apple_financials", "period_start", "period_end")]


def test_a_table_whose_name_is_not_a_plain_identifier_is_skipped() -> None:
    """This module interpolates schema names straight into SQL — it is the one
    place that does not go through ``validate_sql``. An allow-list entry is
    admin-curated, but a quote in a name would still end the quoted identifier."""
    schema = [
        {
            "table": 'evil"; DROP TABLE filings; --',
            "columns": [{"name": "period_start"}, {"name": "period_end"}],
        }
    ]
    assert period_pairs(schema) == []


def test_one_duration_says_nothing() -> None:
    """A table whose facts are all the same length has no trap to warn about,
    and a note that always fires is noise."""
    assert duration_note([(90, 400)]) is None


def test_durations_within_twice_each_other_say_nothing() -> None:
    assert duration_note([(90, 400), (97, 220)]) is None


def test_a_bucket_too_small_to_be_a_shape_is_ignored() -> None:
    """Three stray rows at 400 days do not make a table's periods mixed."""
    assert duration_note([(90, 2479), (400, 3)]) is None


def test_mixed_durations_are_named_with_their_row_counts() -> None:
    """apple_financials as measured on 2026-08-10."""
    note = duration_note([(90, 2479), (363, 1766), (272, 1146), (None, 4897)])
    assert note is not None
    assert "90d (2479 rows)" in note
    assert "363d (1766 rows)" in note
    assert "4897" in note and "instant" in note.lower()
    assert "period_end - period_start" in note


def test_an_interval_difference_is_read_as_days() -> None:
    """Postgres subtracts two ``date`` columns to an integer but two
    ``timestamp`` columns to an interval. Without this the comparison in
    ``duration_note`` raises TypeError — outside the probe's try block, which
    would turn a best-effort hint into a failed report."""
    assert _as_days(datetime.timedelta(days=90)) == 90
    assert _as_days(90) == 90
    assert _as_days(None) is None


def test_a_duration_that_is_neither_a_number_nor_an_interval_is_dropped() -> None:
    assert _as_days("ninety") is None
    assert _as_days(datetime.timedelta(hours=5)) is None


async def test_each_dialect_quotes_its_own_identifiers() -> None:
    """A double-quoted name is a string literal in MySQL, so one shared template
    would make that branch fail into `except` forever while looking implemented."""
    seen: list[str] = []

    async def fake_run(**kwargs: Any) -> QueryResult:
        seen.append(kwargs["sql"])
        return QueryResult(columns=["days", "n"], rows=[])

    schema = [
        {
            "table": "apple_financials",
            "columns": [{"name": "period_start"}, {"name": "period_end"}],
        }
    ]
    with mock.patch.object(report_period_hints, "run_validated_query", fake_run):
        for db_type in (DatabaseType.POSTGRES, DatabaseType.MYSQL):
            await report_period_hints.probe_period_notes(
                conn_params={}, db_type=db_type, exposed_schema=schema
            )
    assert 'FROM "apple_financials"' in seen[0]
    assert "FROM `apple_financials`" in seen[1]


async def test_a_failing_probe_yields_no_note_and_never_raises() -> None:
    """The hint is an optimisation, never a dependency."""

    async def boom(**kwargs: Any) -> QueryResult:
        raise RuntimeError("statement timeout")

    schema = [
        {
            "table": "apple_financials",
            "columns": [{"name": "period_start"}, {"name": "period_end"}],
        }
    ]
    with mock.patch.object(report_period_hints, "run_validated_query", boom):
        notes = await report_period_hints.probe_period_notes(
            conn_params={}, db_type=DatabaseType.POSTGRES, exposed_schema=schema
        )
    assert notes == {}


SEC01_SCHEMA = [
    {
        "table": "apple_financials",
        "columns": [
            {"name": "tag"},
            {"name": "value"},
            {"name": "period_fiscal_year"},
            {"name": "period_start"},
            {"name": "period_end"},
        ],
    },
    {"table": "filings", "columns": [{"name": "accession"}, {"name": "company"}]},
]

# The query that produced Σ 1 205,3 млрд, verbatim.
SEC01_SQL = (
    "SELECT period_fiscal_year AS fiscal_year, SUM(value) AS revenue "
    "FROM apple_financials WHERE tag = 'us-gaap:Revenues' GROUP BY 1 ORDER BY 1"
)


def test_the_sec01_query_is_caught() -> None:
    """The measured defect: aggregates apple_financials, never mentions a
    duration column, and sums quarterly + YTD + annual facts together."""
    assert (
        aggregates_without_duration(
            SEC01_SQL,
            db_type=DatabaseType.POSTGRES,
            mixed_tables={"apple_financials"},
            exposed_schema=SEC01_SCHEMA,
        )
        == "apple_financials"
    )


@pytest.mark.parametrize(
    ("name", "sql"),
    [
        (
            "a duration predicate constrains it",
            "SELECT period_fiscal_year, SUM(value) AS revenue FROM apple_financials "
            "WHERE tag = 'us-gaap:Revenues' AND (period_end - period_start) >= 350 GROUP BY 1",
        ),
        (
            "grouping by the duration separates the kinds",
            "SELECT (period_end - period_start) AS days, SUM(value) AS revenue "
            "FROM apple_financials GROUP BY 1",
        ),
        (
            "a bare mention is enough under the lenient rule",
            "SELECT period_start, period_fiscal_year, SUM(value) AS revenue "
            "FROM apple_financials GROUP BY 1, 2",
        ),
        (
            "nothing is aggregated, so nothing is double-counted",
            "SELECT period_fiscal_year, value FROM apple_financials WHERE tag = 'x'",
        ),
        (
            "the table it aggregates is not a mixed one",
            "SELECT company, COUNT(*) AS n FROM filings GROUP BY 1",
        ),
    ],
)
def test_queries_that_must_not_fire(name: str, sql: str) -> None:
    assert (
        aggregates_without_duration(
            sql,
            db_type=DatabaseType.POSTGRES,
            mixed_tables={"apple_financials"},
            exposed_schema=SEC01_SCHEMA,
        )
        is None
    ), name


def test_no_mixed_tables_means_nothing_to_check() -> None:
    """A schema whose periods are uniform never fires — the probe found nothing."""
    assert (
        aggregates_without_duration(
            SEC01_SQL,
            db_type=DatabaseType.POSTGRES,
            mixed_tables=set(),
            exposed_schema=SEC01_SCHEMA,
        )
        is None
    )


def test_unparseable_sql_is_not_this_module_s_problem() -> None:
    """`validate_sql` owns parse failures and runs first; this must not raise."""
    assert (
        aggregates_without_duration(
            "SELECT FROM WHERE",
            db_type=DatabaseType.POSTGRES,
            mixed_tables={"apple_financials"},
            exposed_schema=SEC01_SCHEMA,
        )
        is None
    )


def test_the_probe_s_keys_are_exactly_the_mixed_tables() -> None:
    """duration_note returns None unless a table holds at least two meaningfully
    different period lengths, so a key exists only for a mixed table."""
    assert mixed_duration_tables({"apple_financials": "period lengths present: 90d"}) == {
        "apple_financials"
    }
    assert mixed_duration_tables({}) == set()
