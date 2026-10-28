"""Turn a natural-language report request into {sql, chart} via the repo's LLM.

The LLM's only contract is a strict JSON object; parsing failures raise
:class:`ReportQueryParseError` whose message doubles as self-repair feedback.
Validation of the SQL itself is the guardrails' job (``sql_guardrails``), not
this module's.
"""

import datetime
import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from contextvault.llm.textgen import generate_text
from contextvault.models import DatabaseType
from contextvault.services.report_format import resolve_language


class ReportQueryParseError(Exception):
    """The LLM's output was not the required JSON contract."""


class ChartSpec(BaseModel):
    """How to draw the result.

    ``y_columns`` carries the *additional* measures for a request that names more
    than one ("volume and price"); they are drawn as panels stacked over one
    shared x-axis, never as a second y-scale. ``series_column`` splits the rows
    into one line per group, which is what a two-company comparison needs. Both
    are optional and default to the previous single-measure, single-series
    behaviour, so a stored ``frozen_chart_spec`` from before they existed still
    validates.
    """

    chart_type: Literal["bar", "line", "pie", "none"]
    x_column: str | None = None
    y_column: str | None = None
    y_columns: list[str] = Field(default_factory=list)
    series_column: str | None = None
    title: str = ""


class ReportQuery(BaseModel):
    """The SQL plus how to present its result.

    ``column_labels`` maps an output column of the SQL to its display heading in
    the report's language. The SQL's own aliases stay ASCII on purpose: a quoted
    non-ASCII identifier is a needless risk in a string that must survive
    ``validate_sql``. A missing or unknown key falls back to the raw column name,
    so a bad map degrades to the previous behaviour instead of failing.
    """

    sql: str = Field(min_length=1)
    chart: ChartSpec
    column_labels: dict[str, str] = Field(default_factory=dict)


def _schema_block(
    exposed_schema: list[dict[str, Any]], period_notes: dict[str, str] | None = None
) -> str:
    """The allow-list as the model sees it, plus anything measured about it.

    A column name says what a fact is called, never how long a period it covers.
    ``period_notes`` carries that one measured line per table; an absent or empty
    map leaves this block byte-identical to what it has always produced.
    """
    notes = period_notes or {}
    lines: list[str] = []
    for table in exposed_schema:
        desc = f" -- {table['description']}" if table.get("description") else ""
        lines.append(f"TABLE {table['table']}{desc}")
        note = notes.get(table["table"])
        if note is not None:
            lines.append(f"  -- {note}")
        for col in table["columns"]:
            col_desc = f" -- {col['description']}" if col.get("description") else ""
            lines.append(f"  {col['name']}{col_desc}")
    return "\n".join(lines)


def build_prompt(
    user_prompt: str,
    *,
    exposed_schema: list[dict[str, Any]],
    db_type: DatabaseType,
    today: datetime.date,
    max_rows: int,
    feedback: list[str],
    period_notes: dict[str, str] | None = None,
    required_columns: Sequence[str] = (),
) -> str:
    """The full instruction the LLM sees; only the exposed schema is revealed.

    ``required_columns`` is the section's output contract (#204): this query is
    one of several whose rows will be concatenated into a single table, so its
    SELECT list is not free. It is empty for the ordinary single-database
    section, which is unchanged.
    """
    feedback_block = ""
    if feedback:
        joined = "\n".join(f"- {f}" for f in feedback)
        feedback_block = (
            f"\nYour previous attempt(s) were rejected. Fix these problems:\n{joined}\n"
        )
    contract_block = ""
    if required_columns:
        listed = ", ".join(required_columns)
        contract_block = (
            "- THE OUTPUT COLUMNS ARE FIXED. This query is one of several whose rows "
            "are put in ONE table, so the SELECT list must return EXACTLY these "
            f"columns, with these names, in this order: {listed}. Alias whatever this "
            "database calls them to these names — the other databases spell them "
            "differently and the table only works if every query agrees. Return no "
            "other columns.\n"
        )
    return (
        f"You write one read-only SQL query for a {db_type.value} database, plus a chart "
        "specification, answering a user's report request.\n\n"
        "You may ONLY use these tables and columns (anything else is forbidden):\n"
        f"{_schema_block(exposed_schema, period_notes)}\n\n"
        "Rules:\n"
        f"{contract_block}"
        "- Output ONLY a JSON object, no prose, no code fences:\n"
        '  {"sql": "...", "chart": {"chart_type": "bar|line|pie|none", '
        '"x_column": "...", "y_column": "...", "y_columns": [], '
        '"series_column": null, "title": "..."}, '
        '"column_labels": {"<sql column>": "<heading>"}}\n'
        f"- The SQL must be exactly one SELECT statement in {db_type.value} syntax. "
        "Never modify data.\n"
        "- For relative date ranges use SQL date arithmetic "
        "(e.g. CURRENT_DATE - INTERVAL '30 days'),\n"
        "  never a hard-coded date, so the query stays correct when re-run later.\n"
        f"- Aggregate/group so the result is a meaningful report of at most {max_rows} rows.\n"
        "- A report is read, not scrolled: prefer an aggregated result of tens of rows to a\n"
        "  raw dump of thousands. Aggregate a daily series to months or quarters unless the\n"
        "  request is specifically about single days.\n"
        "- ORDER BY the x_column ASCENDING for any time series, so the chart reads\n"
        "  left to right.\n"
        '- x_column and y_column must be output column names of the SQL; use chart_type "none"\n'
        "  when no chart makes sense.\n"
        "- When the request names more than one measure (e.g. volume AND price), put the\n"
        "  first in y_column and the rest in y_columns (at most 2 more). They are drawn as\n"
        "  stacked panels sharing the x-axis. Never try to force two measures into one\n"
        "  y_column.\n"
        "- When the rows cover several entities (two companies, several categories) over the\n"
        "  same x values, name the column holding the entity in series_column, so each gets\n"
        "  its own line instead of one line zig-zagging between them.\n"
        '- chart_type "pie" is only for the parts of one whole at one point in time, with at\n'
        "  most 8 positive slices. Never a pie of a time series.\n"
        "- Write the chart title in the language of the user's request.\n"
        "- SQL column aliases must stay ASCII (snake_case). Do NOT translate them.\n"
        "- column_labels translates every output column into a reader-facing heading "
        'in the language of the user\'s request, e.g. {"total_revenue": "Дохід"}.\n'
        f"{feedback_block}\n"
        f"Today's date: {today.isoformat()}\n"
        f"User request: {user_prompt}\n"
    )


def parse_report_query(text: str) -> ReportQuery:
    """Parse the LLM's reply into a :class:`ReportQuery`; tolerate code fences only."""
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ReportQueryParseError("Reply contained no JSON object.")
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReportQueryParseError(f"Reply was not valid JSON: {exc}") from exc
    try:
        return ReportQuery.model_validate(payload)
    except ValidationError as exc:
        raise ReportQueryParseError(f"JSON did not match the contract: {exc}") from exc


async def generate_report_query(
    provider: str,
    api_key: str,
    model: str,
    *,
    base_url: str | None,
    user_prompt: str,
    exposed_schema: list[dict[str, Any]],
    db_type: DatabaseType,
    feedback: list[str],
    max_rows: int = 10_000,
    period_notes: dict[str, str] | None = None,
    required_columns: Sequence[str] = (),
) -> ReportQuery:
    """One generation attempt: prompt → provider → parsed contract."""
    prompt = build_prompt(
        user_prompt,
        exposed_schema=exposed_schema,
        db_type=db_type,
        today=datetime.date.today(),
        max_rows=max_rows,
        feedback=feedback,
        period_notes=period_notes,
        required_columns=required_columns,
    )
    reply = await generate_text(provider, api_key, model, prompt=prompt, base_url=base_url)
    return parse_report_query(reply)


# --------------------------------------------------------------------------- #
# A connection describing itself (#202)
# --------------------------------------------------------------------------- #

# Raised from 240 (#215). At 240 a description had room for the subject matter or
# its coverage, not both — and this is the text #203 routes a report's sections
# on, so a thin one is a worse router as well as a worse label. Doubled rather
# than removed: it is interpolated into the planner prompt once per connection,
# so it stays bounded. The column is `Text`, so nothing here needs a migration.
MAX_DESCRIPTION_CHARS = 480


class DescriptionUnavailable(Exception):
    """The model returned nothing usable for a connection description."""


def schema_fingerprint(exposed_schema: list[dict[str, Any]]) -> str:
    """A stable digest of an allow-list, for deciding a description is stale.

    Keys are sorted, so the digest tracks *what the allow-list says* rather than
    how it happened to be serialised. Without that, a description would be marked
    stale whenever the client or SQLAlchemy wrote the same allow-list back in a
    different key order — and a staleness marker an admin learns to ignore is
    worse than none.

    List order is deliberately NOT sorted away: the tables are shown to the model
    in order, so reordering them is a different prompt.
    """
    canonical = json.dumps(
        exposed_schema, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# The languages a description can be written in, by the code the client sends.
# Named for the model rather than passed as a tag: "Write in uk" is not an
# instruction a model follows reliably.
_LANGUAGE_NAMES = {"en": "English", "uk": "Ukrainian"}


def build_description_prompt(
    exposed_schema: list[dict[str, Any]],
    *,
    db_type: DatabaseType,
    language: str | None = None,
) -> str:
    """Ask for one line describing what this connection holds.

    Built from ``_schema_block`` — the same rendering the query prompt uses — so
    the two cannot drift into describing the allow-list differently. The cap is
    stated to the model as well as enforced on the way in: truncating a sentence
    mid-word produces a worse description than asking for a short one.

    ``language`` is the reader's interface language (#215): this text is shown on
    the Data page, so writing it in English for a Ukrainian interface described
    every database in a language nobody chose. It is resolved through
    ``resolve_language``, the one answer to "what language is this?" in the
    codebase, so an absent, regional (``uk-UA``) or nonsense value degrades to a
    default instead of failing a request.
    """
    written_in = _LANGUAGE_NAMES[resolve_language(language, prompt="")]
    return (
        "You write a one-line description of what a reporting database contains, "
        "for a colleague choosing which database to run a report against.\n\n"
        f"The database is {db_type.value}. These are the only tables and columns "
        "exposed to reports:\n"
        f"{_schema_block(exposed_schema)}\n\n"
        "Rules:\n"
        "- Output ONLY the description. No prose around it, no quotes, no code fences.\n"
        "- One line. No newlines.\n"
        f"- At most {MAX_DESCRIPTION_CHARS} characters.\n"
        "- Say what subject matter it holds, what period it covers if the columns "
        "show one, and which entities or categories appear in it. Two or three "
        "sentences' worth — e.g. \"Apple Inc. quarterly and annual financials, "
        'FY2016-2025, plus daily closing prices".\n'
        f"- Write in {written_in}.\n"
        "- Describe the data, not the schema: no table names, no column names, no "
        "row counts.\n"
    )


def parse_description(text: str) -> str:
    """Normalise a model's answer into the single line that gets stored.

    Models pad — a wrapping quote, a trailing full stop on its own line, an
    explanatory second paragraph. This is stored, shown in a picker and pasted
    into the planner's prompt in #203, so it is normalised once here rather than
    at each of those readers.
    """
    collapsed = " ".join(text.split())
    stripped = collapsed.strip().strip('"').strip("'").strip()
    if not stripped:
        raise DescriptionUnavailable("The model returned no description.")
    return stripped[:MAX_DESCRIPTION_CHARS]


async def generate_connection_description(
    provider: str,
    api_key: str,
    model: str,
    *,
    base_url: str | None,
    exposed_schema: list[dict[str, Any]],
    db_type: DatabaseType,
    language: str | None = None,
) -> str:
    """One generation attempt: allow-list → provider → one stored line."""
    prompt = build_description_prompt(exposed_schema, db_type=db_type, language=language)
    reply = await generate_text(provider, api_key, model, prompt=prompt, base_url=base_url)
    return parse_description(reply)
