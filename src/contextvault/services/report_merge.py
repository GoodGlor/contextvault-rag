"""Concatenate one section's per-database results into a single table (#204).

Two databases cannot be joined by one SQL query, so the join happens here: each
named database runs its own query — its own NL→SQL self-repair loop, its own
``#176``/``#178`` overlap guard, its own ambiguity check — and this module puts
the conforming results in one table with a column saying where each row came
from.

**Everything that judges a single result stays upstream, and that is the point.**
Running the overlap and ambiguity guards per database *before* the merge is not a
check this module performs; it is a consequence of the merge being a layer above
the executor rather than a rewrite of it. The provenance column makes almost any
merged row-set look distinctly keyed, so a within-database duplicate would stop
being visible the moment it was merged — exactly the defect #178 fixed.

Pure and dependency-light, like ``report_format``: no session, no provider, no
matplotlib. The merged chart is redrawn from the merged rows by the caller.
"""

from collections.abc import Sequence
from typing import Any

from contextvault.services.report_execution import QueryResult
from contextvault.services.report_format import label
from contextvault.services.report_planner import PROVENANCE_COLUMN, PlannedSection
from contextvault.services.report_types import SectionResult


def _projected(result: QueryResult, columns: Sequence[str]) -> list[tuple[Any, ...]] | None:
    """The result's rows reduced to ``columns``, **by name**, or None if it cannot.

    By name rather than by position: the executor already re-asks until a query
    conforms to the contract, so this is belt-and-braces against the one bug class
    that would leave no trace in the output — rows misaligned under headings that
    look right.

    A result missing a contracted column returns None and is treated as a database
    that did not answer, rather than being merged under the wrong headings.
    """
    try:
        indexes = [result.columns.index(column) for column in columns]
    except ValueError:
        return None
    return [tuple(row[index] for index in indexes) for row in result.rows]


def merge_section_results(
    parts: Sequence[tuple[str, SectionResult]],
    *,
    planned: PlannedSection,
    language: str,
) -> SectionResult:
    """One section's per-database results as a single section.

    ``parts`` pairs each database's **display name** with what it produced, in the
    order the plan named the databases — which is the order the rows keep. Sorting
    or interleaving them would be inventing an order the data does not have.

    Partial failure is delivered, not hidden: a database that errored, returned no
    rows, or returned a non-conforming result is named in ``notes`` while the rest
    of the table is still built. Only when *no* database produced a data result
    does the section itself fail.
    """
    columns = list(planned.columns)
    rows: list[tuple[Any, ...]] = []
    silent: list[str] = []
    labels: dict[str, str] = {}
    sql_blocks: list[str] = []
    chart_spec: dict[str, Any] | None = None
    overlap: str | None = None
    any_data = False

    for name, part in parts:
        if part.kind != "data" or part.result is None:
            silent.append(name)
            continue
        any_data = True
        if part.generated_sql:
            sql_blocks.append(f"-- {name}\n{part.generated_sql}")
        projected = _projected(part.result, columns)
        if projected is None:
            silent.append(name)
            continue
        if not projected:
            # Reached, ran, matched nothing. Not an error, still a gap in the
            # table the reader is looking at.
            silent.append(name)
            continue
        # First-wins, in plan order: the contract guarantees the columns are the
        # same, so the first database to actually return rows is as good a source
        # for the spec and the headings as any, and it is deterministic.
        if chart_spec is None:
            chart_spec = part.chart_spec
        if overlap is None:
            overlap = part.overlap_warning
        for key, value in part.column_labels.items():
            labels.setdefault(key, value)
        rows.extend((name, *row) for row in projected)

    if not any_data:
        first_name, first_part = parts[0]
        return SectionResult(
            title=planned.title,
            source="database",
            sub_question=planned.sub_question,
            kind="error",
            error=f"{first_name}: {first_part.error or 'no result'}",
        )

    labels[PROVENANCE_COLUMN] = label("source_database", language)
    notes = [label("no_rows_from", language).format(names=", ".join(silent))] if silent else []
    return SectionResult(
        title=planned.title,
        source="database",
        sub_question=planned.sub_question,
        kind="data",
        result=QueryResult(columns=[PROVENANCE_COLUMN, *columns], rows=rows),
        # Every database this section read, in plan order — including the ones
        # that answered with nothing. "Where could these figures have come from"
        # is the question a caption answers, and a database that was asked and
        # was silent is part of that answer (#205).
        databases=[name for name, _ in parts],
        generated_sql="\n\n".join(sql_blocks) or None,
        chart_spec=chart_spec,
        column_labels=labels,
        # No chart here on purpose: it is redrawn from the merged rows by the
        # caller. A per-database chart carried through would show one database's
        # data under the merged section's title.
        notes=notes,
        empty=not rows,
        overlap_warning=overlap,
    )
