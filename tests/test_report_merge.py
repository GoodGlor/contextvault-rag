"""#204: concatenating one section's per-database results into a single table.

The merge is a pure function on executed results, so everything the card worries
about — provenance, partial failure, which spec wins, what the reader is told —
is testable without a database, a provider, or a chart.
"""

from collections.abc import Sequence
from typing import Any

from contextvault.services.report_execution import QueryResult
from contextvault.services.report_merge import merge_section_results
from contextvault.services.report_planner import PROVENANCE_COLUMN, PlannedSection
from contextvault.services.report_types import SectionResult

CONTRACT = ["company", "fiscal_year", "revenue"]

PLANNED = PlannedSection(
    title="Дохід Apple та Microsoft",
    source="database",
    databases=["db-a", "db-b"],
    columns=CONTRACT,
    sub_question="дохід за роками",
)


def _part(
    rows: Sequence[tuple[Any, ...]],
    *,
    columns: list[str] | None = None,
    sql: str = "SELECT 1",
    labels: dict[str, str] | None = None,
    chart: dict[str, object] | None = None,
    overlap: str | None = None,
) -> SectionResult:
    return SectionResult(
        title=PLANNED.title,
        source="database",
        sub_question=PLANNED.sub_question,
        kind="data",
        result=QueryResult(columns=columns or CONTRACT, rows=list(rows)),
        generated_sql=sql,
        column_labels=labels or {},
        chart_spec=chart or {"chart_type": "bar", "x_column": "fiscal_year"},
        overlap_warning=overlap,
    )


def _failed(message: str) -> SectionResult:
    return SectionResult(
        title=PLANNED.title,
        source="database",
        sub_question=PLANNED.sub_question,
        kind="error",
        error=message,
    )


APPLE_ROWS = [("Apple", 2024, 391.0), ("Apple", 2025, 416.2)]
MSFT_ROWS = [("Microsoft", 2024, 245.1), ("Microsoft", 2025, 281.7)]


class TestTheMergedTable:
    def test_rows_from_every_database_land_in_one_result(self) -> None:
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", _part(MSFT_ROWS))],
            planned=PLANNED,
            language="en",
        )
        assert merged.kind == "data"
        assert merged.result is not None
        assert len(merged.result.rows) == 4

    def test_provenance_is_the_first_column_and_names_the_database(self) -> None:
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", _part(MSFT_ROWS))],
            planned=PLANNED,
            language="en",
        )
        assert merged.result is not None
        assert merged.result.columns == [PROVENANCE_COLUMN, *CONTRACT]
        assert [row[0] for row in merged.result.rows] == [
            "Apple DB",
            "Apple DB",
            "Microsoft DB",
            "Microsoft DB",
        ]

    def test_rows_keep_the_order_the_plan_named_the_databases_in(self) -> None:
        """Not sorted, not interleaved: any other order would be invented."""
        merged = merge_section_results(
            [("Microsoft DB", _part(MSFT_ROWS)), ("Apple DB", _part(APPLE_ROWS))],
            planned=PLANNED,
            language="en",
        )
        assert merged.result is not None
        assert merged.result.rows[0][1] == "Microsoft"

    def test_columns_are_taken_by_name_not_by_position(self) -> None:
        """The executor already enforces the contract, so this is belt-and-braces
        against the one bug class that would be invisible in the output: rows
        silently misaligned under the right headings."""
        shuffled = _part([(2024, "Apple", 391.0)], columns=["fiscal_year", "company", "revenue"])
        merged = merge_section_results([("Apple DB", shuffled)], planned=PLANNED, language="en")
        assert merged.result is not None
        assert merged.result.rows == [("Apple DB", "Apple", 2024, 391.0)]

    def test_the_provenance_column_gets_a_localised_heading(self) -> None:
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS))], planned=PLANNED, language="uk"
        )
        assert merged.column_labels[PROVENANCE_COLUMN] == "База даних"


class TestPartialFailure:
    """One database answers, another does not. #185 ruled the all-empty report;
    this is the partially-empty section, which is not the same question."""

    def test_a_database_that_failed_does_not_take_the_section_with_it(self) -> None:
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", _failed("timed out"))],
            planned=PLANNED,
            language="en",
        )
        assert merged.kind == "data"
        assert merged.result is not None
        assert len(merged.result.rows) == 2

    def test_the_reader_is_told_which_database_contributed_nothing(self) -> None:
        """Silently dropping it is the one outcome ruled out — it answers from
        half the data without erroring."""
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", _failed("timed out"))],
            planned=PLANNED,
            language="en",
        )
        assert any("Microsoft DB" in note for note in merged.notes)

    def test_a_database_that_returned_no_rows_is_named_too(self) -> None:
        """Reaching the database and finding nothing is not an error, but it is
        still a gap in the table the reader is looking at."""
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", _part([]))],
            planned=PLANNED,
            language="en",
        )
        assert any("Microsoft DB" in note for note in merged.notes)

    def test_nothing_is_said_when_every_database_answered(self) -> None:
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", _part(MSFT_ROWS))],
            planned=PLANNED,
            language="en",
        )
        assert merged.notes == []

    def test_every_database_failing_is_a_failed_section(self) -> None:
        merged = merge_section_results(
            [("Apple DB", _failed("timed out")), ("Microsoft DB", _failed("no such table"))],
            planned=PLANNED,
            language="en",
        )
        assert merged.kind == "error"
        assert merged.error is not None
        assert "Apple DB" in merged.error and "timed out" in merged.error

    def test_every_database_returning_nothing_is_an_empty_section(self) -> None:
        """Not an error — the query ran. #185 decides what an all-empty report
        does with it, and it needs the flag to do that."""
        merged = merge_section_results(
            [("Apple DB", _part([])), ("Microsoft DB", _part([]))],
            planned=PLANNED,
            language="en",
        )
        assert merged.kind == "data"
        assert merged.empty is True

    def test_a_result_missing_a_contract_column_counts_as_no_answer(self) -> None:
        """Rather than merging it under the wrong headings."""
        wrong = _part([("Apple", 2024)], columns=["company", "fiscal_year"])
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS)), ("Microsoft DB", wrong)],
            planned=PLANNED,
            language="en",
        )
        assert merged.result is not None
        assert len(merged.result.rows) == 2
        assert any("Microsoft DB" in note for note in merged.notes)


class TestWhatTheMergedSectionCarriesForward:
    def test_the_chart_spec_comes_from_the_first_database_with_rows(self) -> None:
        """The contract guarantees identical columns, so a spec valid for one is
        valid for the merged table."""
        merged = merge_section_results(
            [
                ("Apple DB", _part([], chart={"chart_type": "pie"})),
                ("Microsoft DB", _part(MSFT_ROWS, chart={"chart_type": "line"})),
            ],
            planned=PLANNED,
            language="en",
        )
        assert merged.chart_spec is not None
        assert merged.chart_spec["chart_type"] == "line"

    def test_no_chart_is_drawn_by_the_merge_itself(self) -> None:
        """It is redrawn from the merged rows afterwards; a per-database chart
        would show one database's data under the merged section's title."""
        merged = merge_section_results(
            [("Apple DB", _part(APPLE_ROWS))], planned=PLANNED, language="en"
        )
        assert merged.chart_png is None and merged.chart_svg is None

    def test_column_labels_merge_first_wins(self) -> None:
        merged = merge_section_results(
            [
                ("Apple DB", _part(APPLE_ROWS, labels={"revenue": "Дохід"})),
                ("Microsoft DB", _part(MSFT_ROWS, labels={"revenue": "Revenue"})),
            ],
            planned=PLANNED,
            language="en",
        )
        assert merged.column_labels["revenue"] == "Дохід"

    def test_every_database_s_sql_is_kept_for_the_audit(self) -> None:
        merged = merge_section_results(
            [
                ("Apple DB", _part(APPLE_ROWS, sql="SELECT a")),
                ("Microsoft DB", _part(MSFT_ROWS, sql="SELECT b")),
            ],
            planned=PLANNED,
            language="en",
        )
        assert merged.generated_sql is not None
        assert "SELECT a" in merged.generated_sql and "SELECT b" in merged.generated_sql
        assert "Apple DB" in merged.generated_sql  # which SQL ran where

    def test_one_database_s_overlap_warning_governs_the_merged_total(self) -> None:
        """#178's defect is reintroduced the moment a merged table is totalled
        while one of its halves double-counts. The caveat propagates, which is
        what suppresses the total and the chart downstream."""
        merged = merge_section_results(
            [
                ("Apple DB", _part(APPLE_ROWS, overlap="apple_financials")),
                ("Microsoft DB", _part(MSFT_ROWS)),
            ],
            planned=PLANNED,
            language="en",
        )
        assert merged.overlap_warning == "apple_financials"
