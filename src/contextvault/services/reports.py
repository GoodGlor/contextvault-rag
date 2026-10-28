"""Blended report orchestration + the frozen-SQL path.

Mirrors ``services/ingestion.py``: awaitable cores that drive the row's status,
each with a BackgroundTasks seam that opens its own session. A fresh report is
two runs, not one — ``plan_report`` (PLANNING→AWAITING_APPROVAL) produces the
plan and stops for its owner to review, then ``generate_report``
(PROCESSING→DONE|FAILED) executes the plan stored on the row, section by section
from its source (guardrailed SQL or grant-filtered RAG) with partial success. The
split exists because a plan is cheap and a generation run is not, so the
correction belongs before the spend. The frozen-schedule path
(``execute_frozen``) has no plan at all: it re-runs an already-validated single
SQL query with no LLM call. Both self-repair loops (planning, per-section SQL)
give the LLM at most 3 attempts, feeding every rejection back as feedback; each
SQL attempt passes the FULL guardrail stack.
"""

import datetime
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import decrypt
from contextvault.db.session import SessionLocal
from contextvault.models import (
    DatabaseConnection,
    GeneratedReport,
    ReportAsset,
    ReportSchedule,
    ReportStatus,
    Repository,
    Source,
    SourceStatus,
)
from contextvault.services import providers as provider_service
from contextvault.services.ingestion import SessionFactory
from contextvault.services.report_blocks import Block, chart_type_overrides, load_blocks
from contextvault.services.report_chart import render_chart_images
from contextvault.services.report_design import (
    cover_brand_for,
    design_payload,
    load_design,
    style_for,
)
from contextvault.services.report_documents import execute_documents_section
from contextvault.services.report_execution import (
    QueryExecutionError,
    QueryResult,
    run_validated_query,
)
from contextvault.services.report_format import (
    dimension_indexes,
    dimensions_identify_rows,
    label,
    resolve_language,
)
from contextvault.services.report_html import CoverBrand, build_report_html
from contextvault.services.report_llm import ChartSpec, ReportQueryParseError, generate_report_query
from contextvault.services.report_merge import merge_section_results
from contextvault.services.report_period_hints import (
    aggregates_without_duration,
    mixed_duration_tables,
    probe_period_notes,
)
from contextvault.services.report_persist import dump_sections, load_sections
from contextvault.services.report_planner import (
    PlannedSection,
    PlannerDatabase,
    ReportPlan,
    ReportPlanDraft,
    ReportPlanParseError,
    draft_has_blanks,
    finalize_draft,
    generate_replan,
    generate_report_plan,
)
from contextvault.services.report_render import build_report_pdf
from contextvault.services.report_render_pptx import build_report_pptx
from contextvault.services.report_style import ReportStyle, resolve_style
from contextvault.services.report_types import ReportAssetRef, SectionResult
from contextvault.services.sql_guardrails import SQLValidationError, validate_sql

_MAX_ATTEMPTS = 3  # 1 initial + 2 self-repairs

# A full result is stored because the chart draws every row: a capped payload would
# re-render a different chart, which defeats the point. Past this budget the report
# simply is not re-renderable — better than reproducing something subtly different.
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


def _store_sections(
    report: GeneratedReport, *, title: str, language: str, sections: list[SectionResult]
) -> None:
    """Attach the re-render payload to the row, or nothing at all.

    Both callers run this inside their try block, so anything raised here would
    mark an otherwise-good report ``FAILED`` — losing the report to save the
    rebuild. ``encode_value`` is written to be total, and this is the belt to its
    braces: a payload that cannot be serialized degrades to "not re-renderable",
    exactly as an over-budget one does.
    """
    try:
        payload = dump_sections(title=title, language=language, sections=sections)
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError):
        report.sections_data = None
        return
    if len(serialized) > _MAX_PAYLOAD_BYTES:
        report.sections_data = None
        return
    report.sections_data = payload


class ReportGenerationError(Exception):
    """No valid query could be produced within the attempt budget."""


class EmptyReportError(ReportGenerationError):
    """Nothing matched anywhere, so no document was built (#185).

    A subclass rather than a message, because the reader has to be able to tell
    this from a provider outage or a timeout: the row stores
    ``f"{type(exc).__name__}: {exc}"``, so the *name* is what reaches the UI. It
    is still a ``ReportGenerationError``, so every existing handler keeps
    working.

    Raised only when **every** section came back empty. A report with one empty
    section and two full ones is still worth having, and refusing it would throw
    away the two that worked.
    """


def produced_nothing(section: SectionResult) -> bool:
    """Whether a section found anything at all to say.

    An ``error`` section produced no data either, but it is not *empty* in this
    sense — the all-errors check above this one owns that case and says so in
    its own words, which is what keeps a provider outage from being reported to
    the reader as "nothing matched".
    """
    if section.kind == "data":
        return section.result is None or not section.result.rows
    return section.empty


def _conn_params(connection: DatabaseConnection) -> dict[str, Any]:
    return {
        "db_type": connection.db_type,
        "host": connection.host,
        "port": connection.port,
        "database": connection.database,
        "username": connection.username,
        "password": decrypt(connection.password_encrypted),
    }


async def has_done_sources(session: AsyncSession, repository_id: uuid.UUID) -> bool:
    """True when the repository has at least one fully ingested source.

    Only ``DONE`` sources have retrievable chunks — a repository whose uploads all
    failed has nothing a documents section could ground in.
    """
    stmt = (
        sa.select(Source.id)
        .where(Source.repository_id == repository_id, Source.status == SourceStatus.DONE)
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


@dataclass(frozen=True)
class _GenerationContext:
    """Everything the entry points need before they diverge.

    Planning and generation start from the same gates — a configured, answerable
    LLM provider plus at least one data capability — so those checks live here
    once instead of drifting apart in two orchestrators.
    """

    provider: str
    api_key: str
    model: str
    base_url: str | None
    connections: list[DatabaseConnection]
    has_documents: bool

    @property
    def by_id(self) -> dict[str, DatabaseConnection]:
        """The repository's databases, keyed the way a plan names them."""
        return {str(connection.id): connection for connection in self.connections}

    def planner_databases(self) -> list[PlannerDatabase]:
        """What the planner picks from — name, description and allow-list each."""
        return [
            PlannerDatabase(
                id=str(connection.id),
                name=connection.name,
                description=connection.description,
                exposed_schema=connection.exposed_schema,
            )
            for connection in self.connections
        ]


async def _load_context(session: AsyncSession, report: GeneratedReport) -> _GenerationContext:
    repo = await session.get(Repository, report.repository_id)
    if repo is None or repo.llm_provider is None:
        raise ReportGenerationError("Repository is missing its LLM config.")
    if not await provider_service.repo_is_answerable(session, repo):
        raise ReportGenerationError("The repository's LLM provider has no verified key.")
    api_key, base_url = await provider_service.get_call_credentials(session, repo.llm_provider)
    # Every connection the repository owns, not ``report.connection_id`` (#203):
    # the plan is what names the database each section reads, so the whole set is
    # what planning offers and what generation resolves against. The report's own
    # column records which one its SQL was generated against, and is written by
    # generation rather than guessed at creation — see ``generate_report``.
    connections = list(
        (
            await session.execute(
                sa.select(DatabaseConnection)
                .where(DatabaseConnection.repository_id == report.repository_id)
                .order_by(DatabaseConnection.name)
            )
        )
        .scalars()
        .all()
    )
    has_documents = await has_done_sources(session, report.repository_id)
    if not connections and not has_documents:
        raise ReportGenerationError(
            "Repository has no database connection and no ingested sources."
        )
    return _GenerationContext(
        provider=repo.llm_provider,  # LLMProviderName is a StrEnum; plain str downstream
        api_key=api_key,
        model=repo.llm_model or "",
        base_url=base_url,
        connections=connections,
        has_documents=has_documents,
    )


async def _plan_with_repair(
    provider: str,
    api_key: str,
    model: str,
    *,
    base_url: str | None,
    user_prompt: str,
    databases: Sequence[PlannerDatabase],
    has_documents: bool,
) -> ReportPlan:
    """Plan the report, feeding parse/validation errors back (≤3 attempts)."""
    feedback: list[str] = []
    for _ in range(_MAX_ATTEMPTS):
        try:
            return await generate_report_plan(
                provider,
                api_key,
                model,
                base_url=base_url,
                user_prompt=user_prompt,
                databases=databases,
                has_documents=has_documents,
                feedback=feedback,
            )
        except ReportPlanParseError as exc:
            feedback.append(str(exc))
    raise ReportGenerationError(
        "Could not plan the report. Last problem: "
        + (feedback[-1] if feedback else "no attempts succeeded")
    )


async def _replan_with_repair(
    ctx: _GenerationContext, *, user_prompt: str, draft: ReportPlanDraft
) -> ReportPlan:
    """Re-plan, feeding contract violations back (≤3 attempts).

    The commonest rejection is a wrong section count — a model trying to re-add
    the section the user deleted. The feedback names the number it must return.
    """
    feedback: list[str] = []
    for _ in range(_MAX_ATTEMPTS):
        try:
            return await generate_replan(
                ctx.provider,
                ctx.api_key,
                ctx.model,
                base_url=ctx.base_url,
                user_prompt=user_prompt,
                draft=draft,
                databases=ctx.planner_databases(),
                has_documents=ctx.has_documents,
                feedback=feedback,
            )
        except ReportPlanParseError as exc:
            feedback.append(str(exc))
    raise ReportGenerationError(
        "Could not re-plan the report. Last problem: "
        + (feedback[-1] if feedback else "no attempts succeeded")
    )


async def replan_report(
    session: AsyncSession, report: GeneratedReport, draft: ReportPlanDraft
) -> None:
    """Fill the user's blanks and return the plan for review. Never raises.

    The user's own edits are already locked in by ``merge_draft``; this only
    decides whether a provider call is needed at all.

    A failure here is *recoverable*, unlike one while planning for the first time:
    the report already holds a plan its owner could approve as it stands, and
    ``FAILED`` is terminal — no route moves a report out of it, and both plan
    endpoints require ``AWAITING_APPROVAL``. So a provider hiccup, or a model that
    breaks the section-count contract three times running (exactly what deleting a
    section provokes), must not destroy the report. It goes back to
    ``AWAITING_APPROVAL`` with the error on the row; only a report with no plan to
    fall back on fails outright.
    """
    previous_plan = report.plan  # read before any commit/rollback can expire it
    report.status = ReportStatus.PLANNING
    await session.commit()
    try:
        ctx = await _load_context(session, report)
        # Release the pooled app-DB connection before slow LLM work (same
        # discipline as ingestion's OCR path).
        await session.commit()
        if draft_has_blanks(draft):
            plan = await _replan_with_repair(ctx, user_prompt=report.prompt, draft=draft)
            report.plan_rounds += 1
        else:
            # Nothing to fill: storing the draft is the whole job, and charging a
            # round for it would be a lie.
            plan = finalize_draft(
                draft,
                connection_ids=ctx.by_id.keys(),
                has_documents=ctx.has_documents,
            )
        report.plan = plan.model_dump()
        report.status = ReportStatus.AWAITING_APPROVAL
        report.error = None
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — any failure lands on the row, never silent
        await session.rollback()
        # The rollback restored the previous plan; hand the report back to its
        # owner rather than stranding it in the terminal FAILED state.
        report.status = (
            ReportStatus.AWAITING_APPROVAL if previous_plan is not None else ReportStatus.FAILED
        )
        report.error = f"{type(exc).__name__}: {exc}"
        await session.commit()


def _cover_date(report: GeneratedReport) -> datetime.date:
    """The date the cover prints — the row's, or today's if it has none yet.

    ``created_at`` carries a server default, so it is null until the INSERT is
    flushed; the frozen-schedule path renders before that happens and read
    ``None.date()``. Today is not a fudge in that case — the row is being created
    now. On a re-render ``created_at`` comes back from the database, which is what
    keeps a rebuilt cover identical to the original instead of restamped.
    """
    return (report.created_at or datetime.datetime.now(datetime.UTC)).date()


async def _execute_database_section(
    connection: DatabaseConnection | None,
    *,
    provider: str,
    api_key: str,
    model: str,
    base_url: str | None,
    planned: PlannedSection,
    language: str = "en",
    report_title: str | None = None,
    style: ReportStyle | None = None,
    period_notes: dict[str, str] | None = None,
    draw_chart: bool = True,
) -> SectionResult:
    """The existing NL→SQL self-repair loop, scoped to one planned section.

    ``draw_chart=False`` is what a merged section passes (#204): its chart is
    redrawn once from the merged rows, so drawing one per database here would be
    wasted work whose output is discarded.
    """
    style = style or resolve_style(None)
    if connection is None:
        # Since #203 a section names the database it reads, and an approved plan
        # was validated against the repository's connections — so reaching here
        # means that database was deleted between approval and generation. It
        # stays a section-level error rather than a failed report: the other
        # sections may still have something to say (partial success).
        return SectionResult(
            title=planned.title,
            source="database",
            sub_question=planned.sub_question,
            kind="error",
            error="The database this section reads is no longer connected to this repository.",
        )
    feedback: list[str] = []
    # A result worth keeping even though we are about to ask again: one that
    # matched nothing, or one whose rows do not have distinct keys. If the retry
    # does no better, an honest section beats an error, and beats inventing one.
    held: SectionResult | None = None
    for _ in range(_MAX_ATTEMPTS):
        try:
            query = await generate_report_query(
                provider,
                api_key,
                model,
                base_url=base_url,
                user_prompt=planned.sub_question,
                exposed_schema=connection.exposed_schema,
                db_type=connection.db_type,
                feedback=feedback,
                period_notes=period_notes,
                required_columns=planned.columns,
            )
        except ReportQueryParseError as exc:
            feedback.append(str(exc))
            continue
        try:
            safe_sql = validate_sql(
                query.sql, db_type=connection.db_type, exposed_schema=connection.exposed_schema
            )
            # Decided from the SQL, before execution: once the database has summed
            # overlapping rows, one row per year comes back with genuinely distinct
            # keys and nothing downstream can tell it is three times too large.
            overlapping = aggregates_without_duration(
                safe_sql,
                db_type=connection.db_type,
                mixed_tables=mixed_duration_tables(period_notes or {}),
                exposed_schema=connection.exposed_schema,
            )
            result = await run_validated_query(**_conn_params(connection), sql=safe_sql)
            # The output contract (#204), checked before anything is kept: this
            # query's rows will be concatenated with another database's, and a
            # result under different headings cannot be. Re-asking is the same
            # self-repair the empty and ambiguous cases already use.
            missing = [column for column in planned.columns if column not in result.columns]
            if missing:
                feedback.append(_contract_feedback(planned.columns, result.columns))
                continue
            images = (
                None
                if overlapping is not None or not draw_chart
                else render_chart_images(
                    result,
                    query.chart,
                    language=language,
                    column_labels=query.column_labels,
                    section_title=planned.title,
                    report_title=report_title,
                    theme=style.chart,
                )
            )
            section = SectionResult(
                title=planned.title,
                source="database",
                sub_question=planned.sub_question,
                kind="data",
                result=result,
                chart_png=None if images is None else images.png,
                chart_svg=None if images is None else images.svg,
                generated_sql=safe_sql,
                chart_spec=query.chart.model_dump(),
                column_labels=query.column_labels,
                overlap_warning=overlapping,
            )
            if not result.rows:
                # First empty result. The commonest cause is a guessed literal — the
                # schema block names columns but never shows their contents, so a
                # query filters on 'Apple' where the column holds 'Apple Inc.', or on
                # an XBRL tag the company stopped using. Nothing upstream notices:
                # zero rows is not an error, so the report used to be delivered as a
                # success containing the single word "Rows: 0".
                if held is None:
                    held = section
                    feedback.append(_empty_result_feedback(safe_sql))
                    continue
                return section
            if overlapping is not None:
                if held is None:
                    held = section
                    feedback.append(_overlapping_aggregate_feedback(overlapping, period_notes))
                    continue
                return section
            if dimensions_identify_rows(result.columns, result.rows) or held is not None:
                return section
            # Rows that share a key overlap, so this result cannot be totalled or
            # charted honestly. Same contract as an empty one: keep it, ask once
            # more, deliver it if the retry is no better.
            held = section
            feedback.append(_ambiguous_result_feedback(result))
        except (SQLValidationError, QueryExecutionError) as exc:
            feedback.append(f"Query `{query.sql}` was rejected: {exc}")
    if held is not None:
        return held
    return SectionResult(
        title=planned.title,
        source="database",
        sub_question=planned.sub_question,
        kind="error",
        error=(
            "Could not produce a valid query for this section. Last problem: "
            + (feedback[-1] if feedback else "no attempts succeeded")
        ),
    )


def _contract_feedback(required: Sequence[str], returned: Sequence[str]) -> str:
    """Ask again for a result the merge can actually concatenate (#204).

    Names both lists: the model chose aliases that this database's own wording
    suggested, and the fix is a rename, not a different query.
    """
    return (
        f"That query returned the columns ({', '.join(returned)}), but this section's "
        f"rows are merged with another database's and the SELECT list must be exactly "
        f"({', '.join(required)}), with those names, in that order. Alias the columns "
        "this database uses to those names; do not add or drop any."
    )


def _overlapping_aggregate_feedback(table: str, period_notes: dict[str, str] | None) -> str:
    """Ask for one re-think of a query that aggregated overlapping period rows.

    The measured histogram is quoted rather than described: the model cannot see
    the period lengths from a column name, and a generic warning about
    "overlapping data" gives it nothing to act on.
    """
    measured = (period_notes or {}).get(table)
    detail = f" Measured on {table}: {measured}." if measured else ""
    return (
        f"That query aggregates `{table}` without constraining the length of the period each "
        f"row covers, so it adds quarterly, year-to-date and annual facts for the same metric "
        f"together and the total is several times too large.{detail} Constrain the period "
        f"length — for annual figures only, something like "
        f"`(period_end - period_start) >= 350` — or group by the period length so the kinds "
        f"stay in separate rows. Do not simply re-run the same aggregate."
    )


def _ambiguous_result_feedback(result: QueryResult) -> str:
    """Ask for one re-think of a result whose rows do not have distinct keys."""
    dimensions = dimension_indexes(result.columns, result.rows)
    counts: dict[tuple[str, ...], int] = {}
    for row in result.rows:
        key = tuple(str(row[index]) for index in dimensions)
        counts[key] = counts.get(key, 0) + 1
    worst, times = max(counts.items(), key=lambda item: item[1])
    named = ", ".join(worst) or "(no key column)"
    return (
        f"That query returned {len(result.rows)} rows, but they do not have distinct keys: "
        f"{named} appears {times} times. Rows that share a key are alternative measurements "
        "of the same thing, not separate observations \u2014 summing or charting them counts the "
        "same figure more than once. A long-format table often stores quarterly, "
        "year-to-date and annual facts for one metric under the same period label, "
        "distinguished only by the length of the period. Constrain the query so each key "
        "appears once, or add the column that tells the rows apart."
    )


def _empty_result_feedback(sql: str) -> str:
    """Ask for one re-think of a query that ran fine and matched nothing."""
    literals = list(dict.fromkeys(re.findall(r"\'([^\']{1,60})\'", sql)))[:8]
    named = ", ".join(f"'{value}'" for value in literals)
    return (
        "That query was valid but returned NO ROWS. Do not repeat it. The usual cause is a "
        "literal that does not match the data"
        + (f" — you filtered on {named}." if named else ".")
        + " A name column may hold a fuller form than you assumed, a code or tag must match "
        "exactly, and a company may have changed which tag it reports over time. Look the "
        "value up instead of assuming it, widen the filter, or drop the condition. If the "
        "data genuinely holds nothing for this question, return the broadest query that "
        "would show that."
    )


def _section_audit(section: SectionResult) -> dict[str, Any]:
    """The JSONB audit entry for one executed section (admin-visible)."""
    return {
        "title": section.title,
        "source": section.source,
        "sub_question": section.sub_question,
        "status": "error" if section.kind == "error" else "done",
        "error": section.error,
        "generated_sql": section.generated_sql,
        "chart_spec": section.chart_spec,
        # A table name rather than a boolean, which is the whole reason the spec
        # chose one: the audit says which table the query aggregated across
        # overlapping periods, so a "done" section that quietly lost its chart
        # and gained a caveat is explicable from the record alone.
        "overlap_warning": section.overlap_warning,
        "cited_source_ids": [str(ref.source_id) for ref in section.sources],
    }


async def _bind_style(session: AsyncSession, report: GeneratedReport) -> ReportStyle:
    """Resolve the template a report is generated in, and record it on the row.

    Order: a design already on the row (a reader who chose before generation
    finished), then the repository's default, then Кварта. *Any* non-None
    ``template`` on the row short-circuits the repository lookup, including an
    unknown one — consistent with ``report_design``'s read-tolerance rule, but
    it means retiring a template later demotes the rows that wore it to the
    fallback rather than to their repository's chosen default.

    The resolved slug is written back to ``report.design``, which is what makes
    a re-render read the row instead of the repository. The value stored is
    ``style.key`` rather than the slug that was asked for: one that fell back
    must not stay on the row claiming a template the artifacts do not wear.

    Writing this before the renderers run departs from ``_run_and_render``'s
    "nothing is written until every step that can fail has succeeded" rule, and
    deliberately: the bind has to ride the commit that releases the pooled
    connection before slow LLM work (see ``generate_report``), which is long
    before any renderer exists to succeed or fail. A ``design`` left on a row
    that then FAILS is inert — nothing reads it, and FAILED is terminal.
    ``sections_data`` and ``pdf_data`` differ in kind: they mean "there is an
    artifact", so on a failed row they would make it advertise itself as
    re-renderable.
    """
    design = load_design(report.design)
    if design.template is None:
        repo = await session.get(Repository, report.repository_id)
        design = load_design(repo.report_template if repo is not None else None)
    # The whole stored design, not just its template. Tokens live on the row
    # beside the template, and a reader can now choose them *before* generation —
    # so resolving the template alone discarded scale, margins and accent at
    # generation and let them appear from nowhere on the first rerender, which
    # was the only path that called ``style_for``. The resolved template is
    # layered on top so a repository fallback still wins over an absent one,
    # while the tokens keep coming from the report's own row.
    raw: dict[str, Any] = {**(report.design or {})}
    if design.template is not None:
        raw.update(design_payload(design.template))
    style = style_for(raw)
    # Merge rather than replace: the payload now carries a block list too, and a
    # re-plan runs this again on a row that may already have one. Assigning a
    # fresh dict is what makes SQLAlchemy notice the change on a JSONB column.
    report.design = {**(report.design or {}), **design_payload(style.key)}
    return style


async def _run_and_render(
    report: GeneratedReport,
    connection: DatabaseConnection,
    sql: str,
    chart: ChartSpec,
    *,
    style: ReportStyle,
) -> None:
    """Execute validated SQL and attach a single-data-section PDF to the report row.

    Used by the frozen-schedule path (``execute_frozen``); fresh reports render
    through the blended orchestrator instead.

    Nothing is written to the row until every step that can fail has succeeded.
    ``execute_frozen`` commits its row whatever happens (a rollback would expunge
    a row that was only flushed, losing the failure record entirely), so a
    renderer raising halfway used to commit a FAILED report still carrying the
    section payload written just before it — ``can_rerender`` true on a report
    its owner was told had failed, and ``/rerender`` willing to manufacture a PDF
    for it.
    """
    result = await run_validated_query(**_conn_params(connection), sql=sql)
    # The frozen path has no LLM and no self-repair loop, so this cannot ask for
    # a better query — it can only stop presenting a wrong one as sound.
    period_notes = await probe_period_notes(
        conn_params=_conn_params(connection),
        db_type=connection.db_type,
        exposed_schema=connection.exposed_schema,
    )
    overlapping = aggregates_without_duration(
        sql,
        db_type=connection.db_type,
        mixed_tables=mixed_duration_tables(period_notes),
        exposed_schema=connection.exposed_schema,
    )
    # A frozen schedule has no plan to carry a language, so infer it from the
    # prompt that produced it — the report must not switch to English on re-run.
    language = resolve_language(None, prompt=report.prompt)
    title = chart.title or report.prompt
    # Drawn through the same helper a re-render uses, never by a second
    # render_chart_images call: ``keeps_title`` suppresses a chart title that echoes the
    # headings printed above the image, so a call that omits them produces a
    # different figure. Building the section without a chart and passing it
    # through ``_with_chart`` makes the two paths incapable of drifting.
    section = _with_chart(
        SectionResult(
            title=title,
            source="database",
            sub_question=report.prompt,
            kind="data",
            result=result,
            generated_sql=sql,
            chart_spec=chart.model_dump(),
            overlap_warning=overlapping,
        ),
        language=language,
        report_title=title,
        style=style,
    )
    # The frozen path's copy of #185's rule, and it matters more here: a nightly
    # schedule cannot retry, so an empty night would otherwise deliver an empty
    # PDF every morning with nobody watching. Raised before any bytes exist;
    # ``execute_frozen`` turns it into a FAILED row carrying the reason, which is
    # the recorded decision — a silent night would make "did it run?"
    # unanswerable.
    if produced_nothing(section):
        raise EmptyReportError(
            "Nothing in this repository matched the question, so no report was built."
        )

    pdf = build_report_pdf(
        title=title,
        sections=[section],
        language=language,
        style=style,
        report_date=_cover_date(report),
    )
    pptx = build_report_pptx(title=title, sections=[section], language=language, style=style)
    # Everything below this line is assignment: it cannot raise, so the row moves
    # from untouched to complete in one go.
    report.generated_sql = sql
    report.chart_spec = chart.model_dump()
    report.sections = [_section_audit(section)]
    _store_sections(report, title=title, language=language, sections=[section])
    report.pdf_data = pdf
    report.pdf_filename = f"report-{report.id}.pdf"
    report.pptx_data = pptx
    report.pptx_filename = f"report-{report.id}.pptx"
    report.status = ReportStatus.DONE
    report.error = None


async def plan_report(session: AsyncSession, report: GeneratedReport) -> None:
    """Plan the report and stop, leaving it for its owner to review. Never raises.

    Nothing is executed here — a plan is cheap, and a wrong one caught now saves a
    full generation run (up to three NL→SQL calls per section, plus retrieval and
    rendering).
    """
    report.status = ReportStatus.PLANNING
    await session.commit()
    try:
        ctx = await _load_context(session, report)
        # Release the pooled app-DB connection before slow LLM work (same
        # discipline as ingestion's OCR path).
        await session.commit()

        plan = await _plan_with_repair(
            ctx.provider,
            ctx.api_key,
            ctx.model,
            base_url=ctx.base_url,
            user_prompt=report.prompt,
            databases=ctx.planner_databases(),
            has_documents=ctx.has_documents,
        )
        report.plan = plan.model_dump()
        report.plan_rounds += 1
        report.status = ReportStatus.AWAITING_APPROVAL
        report.error = None
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — any failure lands on the row, never silent
        await session.rollback()
        report.status = ReportStatus.FAILED
        report.error = f"{type(exc).__name__}: {exc}"
        await session.commit()


async def _execute_planned_databases(
    planned: PlannedSection,
    *,
    by_id: dict[str, DatabaseConnection],
    ctx: "_GenerationContext",
    language: str,
    title: str,
    style: ReportStyle,
    period_notes_by_database: dict[str, dict[str, str]],
) -> SectionResult:
    """Run a database section against every database it names, and merge (#204).

    One database is the ordinary case and takes the untouched single-database
    path — same call, same result, no merge and no provenance column, so nothing
    about the overwhelmingly common section changes.

    Several databases run the *same* executor once each, which is what keeps the
    #176/#178 overlap guard and the ambiguity check per database and before the
    merge: each result is judged against its own database's period notes and
    schema, exactly as if it were the only one.

    An approved plan was validated against this repository's connections, so a
    missing id means the connection was deleted between approval and generation —
    reported per database, so deleting one of three does not lose the other two.
    """
    databases = planned.databases or [""]
    parts: list[tuple[str, SectionResult]] = []
    merging = len(databases) > 1
    for database in databases:
        connection = by_id.get(database)
        part = await _execute_database_section(
            connection,
            provider=ctx.provider,
            api_key=ctx.api_key,
            model=ctx.model,
            base_url=ctx.base_url,
            planned=planned,
            language=language,
            report_title=title,
            style=style,
            period_notes=period_notes_by_database.get(database, {}),
            draw_chart=not merging,
        )
        if not merging:
            # The ordinary section records its one database too (#205). Nothing
            # renders it — the caption needs more than one — but a field set only
            # sometimes, for no reason visible here, is the more confusing thing.
            name = (
                connection.name if connection is not None else label("database_removed", language)
            )
            return replace(part, databases=[name])
        # The database's own name, never its id: this string is printed in the
        # table and in the note the reader gets, and a UUID tells them less than
        # nothing — the same reason the plan editor's chip prints no raw id. A
        # connection deleted between approval and generation has only its id
        # left, so it is described instead of named.
        parts.append(
            (
                connection.name if connection is not None else label("database_removed", language),
                part,
            )
        )
    merged = merge_section_results(parts, planned=planned, language=language)
    return _with_chart(merged, language=language, report_title=title, style=style)


async def generate_report(session: AsyncSession, report: GeneratedReport) -> None:
    """Execute an approved plan: sections → PDF + PPTX; never raises.

    Each section is answered from its source (the guardrailed SQL path or the
    grant-filtered RAG path) with partial success: a failed section becomes a
    rendered note, and the report FAILS only when every section failed.
    """
    report.status = ReportStatus.PROCESSING
    await session.commit()
    try:
        ctx = await _load_context(session, report)
        # Snapshot the template while the connection is still held: the commit
        # below persists it, and everything after this point is slow LLM work.
        style = await _bind_style(session, report)
        # Release the pooled app-DB connection before slow LLM work (same
        # discipline as ingestion's OCR path); retrieval re-acquires as needed.
        await session.commit()

        if report.plan is None:
            raise ReportGenerationError("This report has no approved plan to execute.")
        plan = ReportPlan.model_validate(report.plan)
        # One language decision for the whole document: headings, column labels,
        # decimal separators and the no-documents note all follow it. The planned
        # answer is validated, with a script check on the prompt as the fallback.
        language = resolve_language(plan.language, prompt=report.prompt)
        title = plan.title.strip() or report.prompt

        # Measured once per *database*, not once per section: the histogram is a
        # full scan and it cannot change between two sections of one run. A
        # failed probe returns {} and the prompt is exactly what it was. Only the
        # databases the plan actually names are probed — a repository may own
        # several and a report may touch one.
        by_id = ctx.by_id
        named_databases = sorted(
            {
                database
                for section in plan.sections
                if section.source == "database"
                for database in section.databases
            }
            & by_id.keys()
        )
        period_notes_by_database = {
            database: await probe_period_notes(
                conn_params=_conn_params(by_id[database]),
                db_type=by_id[database].db_type,
                exposed_schema=by_id[database].exposed_schema,
            )
            for database in named_databases
        }

        results: list[SectionResult] = []
        for planned in plan.sections:
            try:
                if planned.source == "database":
                    section = await _execute_planned_databases(
                        planned,
                        by_id=by_id,
                        ctx=ctx,
                        language=language,
                        title=title,
                        style=style,
                        period_notes_by_database=period_notes_by_database,
                    )
                else:
                    section = await execute_documents_section(
                        session,
                        repository_id=report.repository_id,
                        requested_by=report.requested_by,
                        planned=planned,
                        provider_name=ctx.provider,
                        api_key=ctx.api_key,
                        model=ctx.model,
                        base_url=ctx.base_url,
                        language=language,
                    )
            except Exception as exc:  # noqa: BLE001 — partial success: section → note
                section = SectionResult(
                    title=planned.title,
                    source=planned.source,
                    sub_question=planned.sub_question,
                    kind="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            results.append(section)

        if all(section.kind == "error" for section in results):
            first_error = results[0].error if results else "empty plan"
            raise ReportGenerationError(f"Every section failed. First problem: {first_error}")

        # Nothing matched anywhere: a cover page, a heading and a sentence saying
        # there was nothing to say is not a report (#185). Checked *here* because
        # this is the first point at which "did anything come back?" is
        # answerable for the whole document, and the last one before bytes exist.
        if all(produced_nothing(section) for section in results):
            raise EmptyReportError(
                "Nothing in this repository matched the question, so no report was built."
            )

        # Both formats are built now so a download is a byte read that cannot fail
        # halfway; the reader chooses the format, not the generator.
        report.sections = [_section_audit(section) for section in results]
        # The column records which database this report read, which creation
        # cannot know: since #201 a repository has several, and the plan is what
        # chooses. In plan order, not id order — "the first database this report
        # read" is a fact about the document; the lowest id is not.
        first_database = next(
            (
                section.databases[0]
                for section in plan.sections
                if section.source == "database" and section.databases
            ),
            None,
        )
        if first_database is not None and first_database in by_id:
            report.connection_id = by_id[first_database].id
        _store_sections(report, title=title, language=language, sections=results)
        assets = await _load_assets(session, report.repository_id)
        brand = cover_brand_for(report.design)
        report.pdf_data = build_report_pdf(
            title=title,
            sections=results,
            language=language,
            style=style,
            report_date=_cover_date(report),
            assets=assets,
            brand=brand,
        )
        report.pdf_filename = f"report-{report.id}.pdf"
        report.pptx_data = build_report_pptx(
            title=title, sections=results, language=language, style=style, assets=assets
        )
        report.pptx_filename = f"report-{report.id}.pptx"
        report.status = ReportStatus.DONE
        report.error = None
        await session.commit()
    except Exception as exc:  # noqa: BLE001 — any failure lands on the row, never silent
        await session.rollback()
        report.status = ReportStatus.FAILED
        report.error = f"{type(exc).__name__}: {exc}"
        await session.commit()


async def run_report_generation(
    report_id: uuid.UUID, *, session_factory: SessionFactory = SessionLocal
) -> None:
    """BackgroundTasks entrypoint: fresh session, load, delegate. No-op if deleted."""
    async with session_factory() as session:
        report = await session.get(GeneratedReport, report_id)
        if report is None:
            return
        await generate_report(session, report)


async def run_report_planning(
    report_id: uuid.UUID, *, session_factory: SessionFactory = SessionLocal
) -> None:
    """BackgroundTasks entrypoint for planning: fresh session, load, delegate."""
    async with session_factory() as session:
        report = await session.get(GeneratedReport, report_id)
        if report is None:
            return
        await plan_report(session, report)


async def run_report_replanning(
    report_id: uuid.UUID,
    draft_data: dict[str, Any],
    *,
    session_factory: SessionFactory = SessionLocal,
) -> None:
    """BackgroundTasks entrypoint for re-planning: fresh session, load, delegate.

    The draft crosses as a plain dict because BackgroundTasks arguments outlive the
    request; it is re-validated here rather than trusted.
    """
    async with session_factory() as session:
        report = await session.get(GeneratedReport, report_id)
        if report is None:
            return
        await replan_report(session, report, ReportPlanDraft.model_validate(draft_data))


async def execute_frozen(session: AsyncSession, *, schedule: ReportSchedule) -> GeneratedReport:
    """Run a schedule's frozen SQL as a new report for its owner (no LLM call)."""
    report = GeneratedReport(
        repository_id=schedule.repository_id,
        connection_id=schedule.connection_id,
        requested_by=schedule.owner_id,
        prompt=schedule.prompt,
        schedule_id=schedule.id,
        status=ReportStatus.PROCESSING,
    )
    session.add(report)
    await session.flush()
    try:
        connection = await session.get(DatabaseConnection, schedule.connection_id)
        if connection is None:
            raise ReportGenerationError("The schedule's database connection is gone.")
        chart = ChartSpec.model_validate(schedule.frozen_chart_spec)
        style = await _bind_style(session, report)
        await _run_and_render(report, connection, schedule.frozen_sql, chart, style=style)
    except Exception as exc:  # noqa: BLE001
        # No rollback here, unlike ``generate_report``: that one's row was
        # committed before its try block, while this row has only been flushed —
        # a rollback would expunge it and the FAILED report would simply not
        # exist. ``_run_and_render`` instead writes nothing to the row until
        # every step that can fail has succeeded, so there is nothing partial
        # left to discard. The one exception is deliberate: ``_bind_style``
        # writes ``design`` above, and the commit below persists it on this
        # branch too — inert on a failed row, since nothing reads it.
        report.status = ReportStatus.FAILED
        report.error = f"{type(exc).__name__}: {exc}"
    await session.commit()
    return report


class ReportNotRenderable(Exception):
    """The report carries no stored section data, so it cannot be rebuilt."""


async def rerender_report(
    report_id: uuid.UUID, *, session_factory: SessionFactory = SessionLocal
) -> None:
    """Rebuild both artifacts from stored data. No provider call, no query.

    Charts are redrawn rather than stored: ``render_chart_images`` is deterministic given
    the result and the spec, and a stored PNG would be dead weight beside two
    artifacts already on the row.

    The injectable ``session_factory`` matches ``run_report_generation`` and
    ``run_report_planning`` in this module — it is how the tests reach it.
    """
    async with session_factory() as session:
        doc = await assemble_document(
            session, await session.get(GeneratedReport, report_id), report_id
        )
        report = doc.report
        report.pdf_data = build_report_pdf(
            title=doc.title,
            sections=doc.sections,
            language=doc.language,
            style=doc.style,
            report_date=doc.report_date,
            blocks=doc.blocks,
            assets=doc.assets,
            brand=doc.brand,
        )
        report.pdf_filename = f"report-{report.id}.pdf"
        # Both artifacts are built from the same list. The deck honours it at
        # slide granularity — which blocks exist and where slides break — while
        # the statistics caption and the chart-consumes-its-slide rule stay the
        # deck's own, because a deck is not a paginated document (step 5b).
        report.pptx_data = build_report_pptx(
            title=doc.title,
            sections=doc.sections,
            language=doc.language,
            style=doc.style,
            blocks=doc.blocks,
            assets=doc.assets,
        )
        report.pptx_filename = f"report-{report.id}.pptx"
        await session.commit()


async def preview_report_html(
    report_id: uuid.UUID, *, session_factory: SessionFactory = SessionLocal
) -> str:
    """The design canvas's document: everything ``rerender`` builds, minus the print.

    The same assembly, the same stored data, the same redrawn charts — and then
    it stops. **Nothing is written.** A preview that stored artifacts would let
    merely opening the editor change what a reader downloads, and the editor
    calls this after every debounced edit.

    ⚠️ **Measured, because the obvious framing is wrong.** 5c's numbers — page
    assembly under 6 ms against 257 ms to print — are about *markup*, and markup
    is not what this costs. On a four-section report with four charts (median of
    seven runs, this developer's Mac): redrawing the charts 269 ms, assembling
    the markup 0.5 ms, printing the PDF a further 331 ms. So the canvas is about
    **45% of the print path**, not a rounding error against it — matplotlib
    dominates both, and only the print half is skipped (along with the deck and
    two multi-megabyte column writes, which are not in those numbers).

    That is still worth having, and it is not the 40× the framing suggests. If
    this ever needs to be faster, the redraw is the thing to attack — the markup
    has nothing left in it.
    """
    async with session_factory() as session:
        doc = await assemble_document(
            session, await session.get(GeneratedReport, report_id), report_id
        )
        return build_report_html(
            title=doc.title,
            sections=doc.sections,
            language=doc.language,
            style=doc.style,
            report_date=doc.report_date,
            blocks=doc.blocks,
            assets=doc.assets,
            brand=doc.brand,
            preview=True,
        )


@dataclass(frozen=True)
class AssembledDocument:
    """Everything the renderers need, resolved from one stored report.

    A dataclass rather than a tuple because the printer, the deck and the canvas
    each read a different subset of it, and a positional unpack would let two of
    them disagree about which element was the brand.
    """

    report: GeneratedReport
    title: str
    language: str
    sections: list[SectionResult]
    style: ReportStyle
    blocks: list[Block] | None
    assets: dict[uuid.UUID, ReportAssetRef]
    brand: CoverBrand
    report_date: datetime.date | None


async def assemble_document(
    session: AsyncSession, report: GeneratedReport | None, report_id: uuid.UUID
) -> AssembledDocument:
    """Rebuild a finished report's inputs from storage. No provider call, no query.

    Extracted so the printed artifact and the canvas cannot diverge. Duplicating
    it would recreate — one level up — exactly the second layout system spec
    decision 1 refuses: the canvas would be a different document that merely
    looked like the printed one, and every later fix would have two homes.

    Charts are redrawn rather than stored: ``render_chart_images`` is
    deterministic given the result and the spec, and a stored PNG would be dead
    weight beside two artifacts already on the row. **A section loaded from
    storage carries no ``chart_svg``** — ``get_report_design`` says so in its own
    comment — so a caller that skipped this would show a report with its figures
    missing beside a PDF that plainly has them.
    """
    if report is None or not report.sections_data:
        raise ReportNotRenderable(str(report_id))
    # Only a finished report may be rebuilt. Section data is written before the
    # renderers run, so a report that failed while rendering could carry a
    # payload; rebuilding it would hand its owner a PDF for a report they were
    # told had failed.
    if report.status is not ReportStatus.DONE:
        raise ReportNotRenderable(str(report_id))
    title, language, sections = load_sections(report.sections_data)
    # The row, never the repository. Generation snapshotted the template onto
    # ``design``; resolving the repository here instead would let a changed
    # default silently restyle a finished report, which is the one thing a
    # rebuild-from-storage promises not to do.
    style = style_for(report.design)
    blocks = load_blocks(report.design)
    overrides = chart_type_overrides(blocks)
    return AssembledDocument(
        report=report,
        title=title,
        language=language,
        sections=[
            _with_chart(
                section,
                language=language,
                report_title=title,
                style=style,
                chart_type=overrides.get(index),
            )
            for index, section in enumerate(sections)
        ],
        style=style,
        blocks=blocks,
        assets=await _load_assets(session, report.repository_id),
        brand=cover_brand_for(report.design),
        report_date=_cover_date(report),
    )


async def _load_assets(
    session: AsyncSession, repository_id: uuid.UUID
) -> dict[uuid.UUID, ReportAssetRef]:
    """Every image this repository's reports may print, keyed by id.

    Loaded here and handed to the renderers as bytes: a printer that took a
    session would be a database client, and both renderers are called from a
    background task, from ``rerender`` and from the preview.

    Scoped to the repository, which is also the access-control boundary — the
    write gate refuses a block naming an asset outside it, and this read simply
    cannot see one.
    """
    rows = await session.scalars(
        sa.select(ReportAsset).where(ReportAsset.repository_id == repository_id)
    )
    return {row.id: ReportAssetRef(mime=row.mime, data=row.data) for row in rows}


def _with_chart(
    section: SectionResult,
    *,
    language: str,
    report_title: str,
    style: ReportStyle,
    chart_type: str | None = None,
) -> SectionResult:
    if (
        section.kind != "data"
        or section.result is None
        or not section.chart_spec
        or section.overlap_warning is not None
    ):
        return section
    spec = ChartSpec(**section.chart_spec)
    if chart_type is not None:
        # The reader's choice, already gated on write against what the result can
        # support. The stored spec is not rewritten: the override belongs to the
        # design, and a regenerated report should get the model's judgement again.
        spec = spec.model_copy(update={"chart_type": chart_type})
    images = render_chart_images(
        section.result,
        spec,
        language=language,
        column_labels=section.column_labels,
        section_title=section.title,
        report_title=report_title,
        theme=style.chart,
    )
    if images is None:
        return replace(section, chart_png=None, chart_svg=None)
    return replace(section, chart_png=images.png, chart_svg=images.svg)
