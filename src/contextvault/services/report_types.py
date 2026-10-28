"""Shared shapes the blended-report pipeline passes between stages.

``SectionResult`` is the executed outcome of one planned section, uniform across
the database executor, the documents executor, and the renderer — so the
orchestrator and the PDF builder never care which source produced a section.
Kept dependency-light (no matplotlib/fpdf imports) so every stage can import it.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from contextvault.services.report_execution import QueryResult


@dataclass(frozen=True)
class ReportAssetRef:
    """An uploaded image's bytes, as either renderer needs them.

    Lives here rather than in one renderer because **both** need it, and this is
    the module they already share — the document renderer importing the deck's
    types, or the reverse, would couple two artifacts that have no business
    knowing about each other.

    The renderers take **bytes, never a session**. They are called from the
    generation background task, from ``rerender`` and from the editor preview,
    each of which already owns its own loading; handing a session in would make
    a printer a database client.
    """

    mime: str
    data: bytes


@dataclass(frozen=True)
class SectionSourceRef:
    """One resolved citation target: the ``[n]`` marker and the source it names."""

    number: int
    title: str
    source_id: uuid.UUID


@dataclass(frozen=True)
class SectionResult:
    """The executed outcome of one planned section, ready to render.

    ``kind`` is what the renderer switches on: ``data`` (DB rows + optional
    chart), ``text`` (grounded prose + per-section sources), or ``error`` (the
    section failed; the report renders a short note and continues — partial
    success). ``generated_sql``/``chart_spec``/``sources`` feed the ``sections``
    JSONB audit on the report row.
    """

    title: str
    source: Literal["database", "documents"]
    sub_question: str
    kind: Literal["data", "text", "error"]
    result: QueryResult | None = None
    chart_png: bytes | None = None
    # The same chart as vectors, for the HTML/PDF path; the PPTX path needs the
    # raster one. Neither is persisted: both are redrawn from the stored result
    # and spec, so ``report_persist`` carries no image bytes at all.
    chart_svg: bytes | None = None
    generated_sql: str | None = None
    chart_spec: dict[str, Any] | None = None
    prose: str | None = None
    sources: list[SectionSourceRef] = field(default_factory=list)
    error: str | None = None
    # Whether this section found nothing at all — set by the executor that knows,
    # never inferred downstream (#185). A documents section that found nothing
    # renders the localised ``no_documents`` note and carries no sources, but so
    # can a genuine answer the model wrote without citing anything, and comparing
    # prose against a translated string to tell them apart is not a test, it is a
    # coincidence. A ``data`` section needs no flag — zero rows is zero rows —
    # and ``produced_nothing`` reads the two together.
    #
    # Not persisted: ``report_persist`` rebuilds sections for a *re-render*, and
    # by then the report exists because it had data at generation.
    empty: bool = False
    # Display headings for the result's columns, keyed by SQL column name. Empty
    # for document sections and whenever the model supplied none.
    column_labels: dict[str, str] = field(default_factory=dict)
    # The display names of the databases this section read, in plan order (#205).
    # Set by the executor that knows, for every database section — single or
    # merged — because a field populated only sometimes, for no reason visible in
    # the code, is the more confusing artifact.
    #
    # **Rendered only when there is more than one.** A single-database section's
    # figures all came from one place, so captioning them would add furniture to
    # every report that already exists. The data is honest; the render rule is
    # the scoping.
    #
    # Carried here rather than read back out of the result's provenance column:
    # that would couple every renderer to PROVENANCE_COLUMN, and it would give
    # nothing at all to a design that keeps the chart and drops the table —
    # which is the case this exists for.
    #
    # Persisted, like ``notes`` and unlike ``empty``.
    databases: list[str] = field(default_factory=list)
    # Reader-facing caveats about how this section was built, already localised.
    # Today only the merge writes them (#204), to name the databases that
    # contributed no rows: a section built from two of three databases is honest
    # only if it says so, and silently dropping one answers from half the data
    # without erroring.
    #
    # Deliberately NOT ``overlap_warning``, which suppresses the chart. A partial
    # merge's chart is still honest about the databases that did answer, so these
    # notes print beside it rather than instead of it.
    notes: list[str] = field(default_factory=list)
    # The mixed-duration table this section's query aggregated without
    # constraining the period length, or None. Set before execution from the SQL
    # alone (#178): once the database has summed overlapping rows, nothing in the
    # result can reveal it. When set, the renderer prints a caveat and no chart.
    overlap_warning: str | None = None
