"""Plan a blended report: one LLM call → ordered sections routed to a source.

The planner sees only what the repository *has* — the DB's exposed schema (if a
connection exists) and whether a document knowledge base exists — never document
contents. Its output is a strict JSON contract; parse/validation failures raise
:class:`ReportPlanParseError` whose message doubles as self-repair feedback
(mirroring ``report_llm``'s contract for SQL generation).
"""

import json
from collections.abc import Collection, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from contextvault.llm.textgen import generate_text
from contextvault.services.report_llm import _schema_block

MAX_SECTIONS = 6  # bounds LLM cost and PDF size

MAX_DATABASES_PER_SECTION = 3
"""How many databases one section may read and merge (#204, raised from #203's 1).

A ceiling rather than no ceiling: each named database is a full NL→SQL
self-repair loop of up to three provider calls, so this number is latency and
spend, not taste. #203 made the field a list precisely so raising this constant
is the whole change — no stored plan is migrated a second time.
"""

MAX_CONTRACT_COLUMNS = 12
"""How many columns one section's output contract may declare.

The names become SQL aliases in every one of the section's queries and prompt
text in each; the cap bounds both. Twelve is comfortably past what a readable
table or chart uses (``_MAX_TABLE_ROWS``'s neighbour in spirit).
"""

PROVENANCE_COLUMN = "source_database"
"""The column the merge prepends, naming which database each row came from.

Defined here, in the contract module, because the plan is where column names are
chosen — and therefore where a plan claiming this name has to be refused. The
merge imports it from here rather than the reverse, so this module stays free of
the execution stack.
"""


class ReportPlanParseError(Exception):
    """The LLM's plan output was not the required JSON contract."""


Source = Literal["database", "documents"]
"""A source that can actually be executed."""

DraftSource = Source | Literal[""]
"""A source mid-edit. ``""`` means "not chosen yet" — the model fills it on the
next re-plan, exactly as it fills a blank title."""


class PlannerDatabase(BaseModel):
    """One connection as the planner sees it (#203).

    A repository owns many connections since #201, so "the database" is no longer
    a thing the pipeline can resolve on its own — the plan names which one each
    section needs, and this is what it picks from.

    ``description`` (#202) is an *aid*, never a gate: it only exists after an
    admin introspects and curates, so a connection without one must still be
    offered or curating a database would silently exclude it from every report.
    ``name`` and the schema block are always there to route on.
    """

    id: str
    name: str
    description: str = ""
    exposed_schema: list[dict[str, Any]] = Field(default_factory=list)


class PlannedSection(BaseModel):
    """One executable section.

    ``sub_question`` is length-capped because since the plan review loop these
    values also arrive from ``/replan`` and ``/approve`` as untrusted caller
    input, not only from the LLM, and are persisted to JSONB and fed back into a
    prompt.

    ``databases`` holds connection ids as strings, not ``UUID``: this model is
    dumped straight into a JSONB column, where a ``UUID`` is not serialisable.

    **It defaults to empty rather than being required, and that is deliberate.**
    This model also parses plans *back out* of the database, including every plan
    stored before #203 existed — none of which carry the key. Making it required
    would turn reading an old report into a 500 on the list endpoint. The routing
    rules are therefore enforced where input is untrusted (:func:`parse_report_plan`
    for the LLM's output, :func:`finalize_draft` for a caller's draft, and
    :func:`check_sources_available` for both), not on the model that also reads
    the app's own history. Be strict at the trust boundary, liberal with your own
    stored data.

    ``columns`` is the section's **output contract** (#204): the SELECT list every
    one of its queries must return, in order, so results from different databases
    can be concatenated. It is required only of a section that names more than one
    database, and it defaults for exactly the reason ``databases`` does — this
    model parses back plans written before the field existed. Two databases rarely
    spell the same thing the same way, which is why the shape is agreed up front
    rather than discovered from whichever result arrives first.
    """

    title: str = Field(min_length=1)
    source: Source
    databases: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    sub_question: str = Field(min_length=1, max_length=2000)


class ReportPlan(BaseModel):
    """The plan drives the whole report, including how it is *presented*.

    ``title`` exists because the renderer previously fell back to printing the
    user's prompt as the heading and then again as body text. ``language`` is the
    one language decision in the pipeline: everything the reader sees — headings,
    column labels, the no-documents note, decimal separators — follows it.
    """

    title: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=16)
    sections: list[PlannedSection] = Field(min_length=1, max_length=MAX_SECTIONS)


class DraftSection(BaseModel):
    """One section as the *user* left it — blanks allowed, unlike ``PlannedSection``.

    A blank is a request: "fill this in for me". ``source`` is blank on a section
    the reader added: the plan chooses sources, so there is no control for them to
    set one with, and the next re-plan is what fills it. It is never
    blank on a section the model produced.

    An empty ``databases`` is a blank of exactly that kind, which is why the
    availability check lets one through and only :func:`finalize_draft` refuses a
    still-empty one — the same division of labour a blank ``source`` already has.
    """

    title: str = Field(default="", max_length=300)
    source: DraftSource = ""
    databases: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    sub_question: str = Field(default="", max_length=2000)


class ReportPlanDraft(BaseModel):
    """A plan mid-edit. The lenient transport shape; ``ReportPlan`` is the strict
    executable one, and :func:`finalize_draft` is the only bridge between them.

    This is the body of both ``/replan`` and ``/approve``, so its length caps are
    the only bound on what a caller can persist and put into a prompt."""

    title: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=16)
    sections: list[DraftSection] = Field(min_length=1, max_length=MAX_SECTIONS)


def _blank(value: str) -> bool:
    return value.strip() == ""


def draft_has_blanks(draft: ReportPlanDraft) -> bool:
    """True when the model has something to fill.

    ``language`` is excluded on purpose: ``resolve_language`` already falls back to
    the prompt's script, so a blank one needs no LLM round.
    """
    if _blank(draft.title):
        return True
    return any(
        _blank(s.title)
        or _blank(s.source)
        or _blank(s.sub_question)
        or _unrouted(s)
        or _uncontracted(s)
        for s in draft.sections
    )


def _unrouted(section: DraftSection) -> bool:
    """A database section that has not been told which database (#203).

    Only meaningful once a source has been chosen: a section with a blank source
    is already a blank, and asking which database it reads is premature.
    """
    return section.source == "database" and not section.databases


def _uncontracted(section: DraftSection | PlannedSection) -> bool:
    """A section that merges several databases but has not agreed a shape (#204).

    A blank of exactly the same kind as :func:`_unrouted`: the next re-plan fills
    it, and only :func:`finalize_draft` refuses one that survived to approval. A
    single-database section owes no contract — there is nothing to concatenate it
    with — so this is False for the overwhelmingly common section.
    """
    return len(section.databases) > 1 and not section.columns


def _prefer(user: str, model: str) -> str:
    """The lock, in one line: the user's value wins wherever they supplied one."""
    return user.strip() if not _blank(user) else model.strip()


def _prefer_source(user: DraftSource, model: DraftSource) -> DraftSource:
    """Same rule as :func:`_prefer`, kept separate so the literal type survives.

    ``_prefer`` returns ``str``, which would widen the field and fail strict mypy.
    """
    return user if not _blank(user) else model


def merge_draft(draft: ReportPlanDraft, reply: ReportPlanDraft) -> ReportPlanDraft:
    """Overlay the model's reply onto the user's draft, user-first.

    Convergence is a property of this function, not of the prompt: a model that
    rewrites a supplied field has its rewrite discarded here, and section identity
    is positional so nothing can be added or removed. This function enforces its
    own section-count contract — a model that adds, drops, or reorders sections is
    reported back through :class:`ReportPlanParseError` rather than crashing the
    caller, since the reply's section count is model-controlled and therefore the
    single most likely adversarial input. ``strict=True`` on the zip below is a
    belt-and-braces assertion for the same invariant.
    """
    if len(reply.sections) != len(draft.sections):
        raise ReportPlanParseError(
            f"Returned {len(reply.sections)} sections, but the plan has "
            f"{len(draft.sections)}. Return exactly the same sections, in the same "
            "order; never add or remove one."
        )
    return ReportPlanDraft(
        title=_prefer(draft.title, reply.title),
        language=_prefer(draft.language, reply.language),
        sections=[
            DraftSection(
                title=_prefer(d.title, r.title),
                source=_prefer_source(d.source, r.source),
                # Same user-first rule, on a list: a routing the draft already
                # carries survives the round, and an empty one is the model's to
                # fill. #186's lock, one level finer.
                databases=d.databases or r.databases,
                # Same rule again for the output contract (#204): a shape the
                # draft already declares is the user's, and an empty one is the
                # model's to fill.
                columns=d.columns or r.columns,
                sub_question=_prefer(d.sub_question, r.sub_question),
            )
            for d, r in zip(draft.sections, reply.sections, strict=True)
        ],
    )


def finalize_draft(
    draft: ReportPlanDraft, *, connection_ids: Collection[str], has_documents: bool
) -> ReportPlan:
    """Turn a complete draft into an executable plan, or say exactly what is missing.

    Blanks are a re-plan instruction, so reaching here with one is a contract error
    rather than something generation should paper over.
    """
    if _blank(draft.title):
        raise ReportPlanParseError("The report needs a title.")
    sections: list[PlannedSection] = []
    for index, section in enumerate(draft.sections, start=1):
        if _blank(section.title):
            raise ReportPlanParseError(f"Section {index} needs a title.")
        if _blank(section.sub_question):
            raise ReportPlanParseError(f'Section {index} ("{section.title}") needs a sub-question.')
        source = section.source
        if source == "":
            raise ReportPlanParseError(f'Section {index} ("{section.title}") needs a source.')
        if _unrouted(section):
            raise ReportPlanParseError(
                f'Section {index} ("{section.title}") needs a database to read.'
            )
        if _uncontracted(section):
            raise ReportPlanParseError(
                f'Section {index} ("{section.title}") {_contract_required(section)}'
            )
        try:
            sections.append(
                PlannedSection(
                    title=section.title.strip(),
                    source=source,
                    databases=section.databases,
                    columns=section.columns,
                    sub_question=section.sub_question.strip(),
                )
            )
        except ValidationError as exc:
            # Belt-and-braces: the blank checks above already cover every field
            # ``PlannedSection`` constrains. It stays because the named-section
            # message is what reaches the model as self-repair feedback, and a
            # raw pydantic dump escaping here would waste a repair round.
            raise ReportPlanParseError(
                f'Section {index} ("{section.title}") is not executable: {exc}'
            ) from exc
    plan = ReportPlan(
        title=draft.title.strip(),
        language=draft.language.strip(),
        sections=sections,
    )
    check_sources_available(
        plan.sections, connection_ids=connection_ids, has_documents=has_documents
    )
    return plan


def _contract_required(section: DraftSection | PlannedSection) -> str:
    """The one wording for "this section owes an output contract".

    Written once because it reaches the model as self-repair feedback from three
    different paths, and feedback that only refuses costs a round.
    """
    return (
        f"reads {len(section.databases)} databases, so it must list the columns every "
        'one of its queries returns, in "columns" — the same columns, in the same '
        "order, from each database, so their rows can be put in one table."
    )


def _sources_block(databases: Sequence[PlannerDatabase], *, has_documents: bool) -> str:
    """The available sources, with every database named, described and spelled out.

    Each connection carries its **whole** allow-list, not just its name and
    description. The description is what makes routing cheap; the schema is what
    keeps a sub-question using the wording the data actually uses — an
    instruction a hundred-report run already showed earning its place. A
    connection nobody has described yet is still listed, on its name and schema
    (#202: absent is not unusable).

    The prompt therefore grows with the number of connections. That is bounded by
    what an admin curated, and a single large allow-list already carried the same
    cost before several were possible.
    """
    sources: list[str] = []
    if databases:
        entries = "\n".join(
            f'  - id "{database.id}" — {database.name}'
            + (f": {database.description.strip()}" if database.description.strip() else "")
            + "\n"
            + _schema_block(database.exposed_schema)
            for database in databases
        )
        sources.append(
            '- "database": read-only SQL databases. Use them for numbers, aggregates, '
            "date ranges, and trends. Each one, with its tables and columns:\n" + entries
        )
    if has_documents:
        sources.append(
            '- "documents": a knowledge base of ingested documents, notes, and web '
            "pages. Use it for policies, definitions, explanations, and narrative "
            "context."
        )
    return "\n".join(sources)


def build_plan_prompt(
    user_prompt: str,
    *,
    databases: Sequence[PlannerDatabase],
    has_documents: bool,
    feedback: list[str],
) -> str:
    """The full planning instruction; only available sources are offered."""
    feedback_block = ""
    if feedback:
        joined = "\n".join(f"- {f}" for f in feedback)
        feedback_block = (
            f"\nYour previous attempt(s) were rejected. Fix these problems:\n{joined}\n"
        )
    return (
        "You plan a report that will be generated section by section. Break the "
        f"user's request into at most {MAX_SECTIONS} focused sections, each answered "
        "from ONE of the available sources.\n\n"
        "Available sources:\n" + _sources_block(databases, has_documents=has_documents) + "\n\n"
        "Rules:\n"
        "- Output ONLY a JSON object, no prose, no code fences:\n"
        '  {"title": "...", "language": "uk|en", "sections": [{"title": "...", '
        '"source": "database|documents", "databases": ["<id>"], "columns": [], '
        '"sub_question": "..."}]}\n'
        "- Use ONLY the sources listed above; never invent a source.\n"
        '- For a "database" section, "databases" must hold the database ids that can '
        f"answer that sub-question — at least one, at most {MAX_DATABASES_PER_SECTION}. "
        "Choose them from each database's name, description and tables; never invent "
        "an id.\n"
        '- For a "documents" section, "databases" and "columns" must both be empty '
        "lists.\n"
        '- Name ONE database in "databases" whenever one can answer the sub-question, '
        'and leave "columns" empty. That is the normal section.\n'
        "- Name SEVERAL databases only when the sub-question needs figures that live "
        'in different databases. Then "columns" must list the columns every one of '
        "those queries returns — the same columns, in the same order, from each "
        'database — because their rows are put in one table. Example: "columns": '
        '["company", "fiscal_year", "revenue"]. Use short lower-case names that each '
        "database can produce, and never use the name "
        f'"{PROVENANCE_COLUMN}" — the report adds that column itself to say which '
        "database each row came from.\n"
        "- Each sub_question must be a self-contained question that one source can "
        "answer on its own.\n"
        "- Write titles and sub-questions in the language of the user's request.\n"
        '- "title" is the report\'s own title: a short descriptive heading, NOT a '
        "restatement of the request and never a question.\n"
        '- "language" is the ISO 639-1 code of the user\'s request: uk or en.\n'
        "- Prefer fewer, stronger sections over many thin ones.\n"
        # A hundred-report run showed this instruction earning its place: asked to
        # compare Apple with Microsoft, the planner returned "Apple ROA", "Apple
        # ROE", "Microsoft ROA", "Microsoft ROE" — four sections that compare
        # nothing and leave the reader to do the work the report was for. The
        # generator can already put several entities in one result and draw one
        # line each, so the comparison belongs in a single section.
        # #203 had to except entities in different databases from this rule, because
        # a section could read only one. #204 deleted that exception rather than
        # keeping it: a section now merges, so the unqualified rule is satisfiable
        # again — and leaving the exception in would have kept the planner splitting
        # the exact comparison this card exists to join.
        "- When the request compares entities (two companies, several categories, "
        "this year against last), write ONE section that asks for them TOGETHER — a "
        "single sub-question returning all the entities side by side, so they can be "
        "charted against each other. Do NOT write one section per entity: four "
        "sections each describing one company is not a comparison. This holds whether "
        "the entities share a database or sit in different ones: if they are in "
        'different databases, name them all in "databases" and agree the "columns".\n'
        "- A sub-question that names a specific value must use the wording the data "
        "actually uses; if you are unsure of a code, label or category name, ask for "
        "it to be looked up in the same section rather than guessing it.\n"
        f"{feedback_block}\n"
        f"User request: {user_prompt}\n"
    )


def check_sources_available(
    sections: Sequence[PlannedSection] | Sequence[DraftSection],
    *,
    connection_ids: Collection[str],
    has_documents: bool,
) -> None:
    """Every section must name a source — and a database — this repository has.

    Shared by every path that can put a source in front of this check: the LLM's
    own output — checked directly in ``parse_report_plan`` (the initial plan) and
    via ``finalize_draft`` inside ``parse_replan`` (a re-plan reply) — and the
    API's untrusted-input paths (``/replan`` calls this directly; ``/approve``
    calls it via ``finalize_draft``), which re-validate a hand-crafted draft
    before spending any provider call on it or dispatching generation. Every
    rejection message exists exactly once, here.

    ``connection_ids`` replaced a ``has_database`` boolean in #203: with several
    connections per repository the two would have been separate facts that could
    disagree, and the id check is what stops a hand-crafted ``/replan`` or
    ``/approve`` body from routing a section at *another* repository's database —
    the same reason #201's routes resolve by the (repository, connection) pair
    rather than by connection id alone.

    An **empty** ``databases`` is not rejected here: on a draft it is a blank the
    next re-plan fills, exactly like a blank ``source``. ``finalize_draft`` is
    what refuses a still-unrouted section at approval.
    """
    known = set(connection_ids)
    for section in sections:
        if section.source == "database":
            if not known:
                raise ReportPlanParseError(
                    f'Section "{section.title}" uses source "database", but there is '
                    "no database available for this repository."
                )
            if any(database not in known for database in section.databases):
                raise ReportPlanParseError(
                    f'Section "{section.title}" names a database that does not belong '
                    "to this repository. Use only the database ids listed above."
                )
            if len(section.databases) > MAX_DATABASES_PER_SECTION:
                raise ReportPlanParseError(
                    f'Section "{section.title}" names {len(section.databases)} databases, '
                    f"but one section reads at most {MAX_DATABASES_PER_SECTION} databases. "
                    "Split it into sections that each read no more than that."
                )
            _check_contract(section)
        if section.source == "documents":
            if not has_documents:
                raise ReportPlanParseError(
                    f'Section "{section.title}" uses source "documents", but there are '
                    "no documents available for this repository."
                )
            if section.databases:
                raise ReportPlanParseError(
                    f'Section "{section.title}" uses source "documents", so it must not '
                    'name a database. Leave "databases" empty.'
                )
            if section.columns:
                raise ReportPlanParseError(
                    f'Section "{section.title}" uses source "documents", so it must not '
                    'list columns. An output contract shapes SQL results; leave "columns" '
                    "empty."
                )


def _check_contract(section: PlannedSection | DraftSection) -> None:
    """The output contract's own rules — shape only, never "is it present".

    Presence is a *blank*, handled by :func:`_uncontracted` and refused at
    approval; these are the rules a contract must obey wherever it appears, on a
    draft mid-edit as much as on an executable plan.
    """
    if len(section.columns) > MAX_CONTRACT_COLUMNS:
        raise ReportPlanParseError(
            f'Section "{section.title}" lists {len(section.columns)} columns, but a '
            f"section may return at most {MAX_CONTRACT_COLUMNS}. Ask for fewer."
        )
    if len(set(section.columns)) != len(section.columns):
        raise ReportPlanParseError(
            f'Section "{section.title}" names the same column twice. Every column in '
            '"columns" must be distinct — they become the result\'s headings.'
        )
    if PROVENANCE_COLUMN in section.columns:
        # Refused rather than renamed: the report adds this column itself, and a
        # silent rename would leave the chart spec naming a column that moved.
        raise ReportPlanParseError(
            f'Section "{section.title}" uses the reserved column name '
            f'"{PROVENANCE_COLUMN}". The report adds that column itself to say which '
            "database each row came from; choose another name."
        )


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON object out of a reply that may be wrapped in prose or fences."""
    stripped = text.strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ReportPlanParseError("Reply contained no JSON object.")
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ReportPlanParseError(f"Reply was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportPlanParseError("Reply's JSON was not an object.")
    return payload


def parse_report_plan(
    text: str, *, connection_ids: Collection[str], has_documents: bool
) -> ReportPlan:
    """Parse the LLM's reply into a validated :class:`ReportPlan`."""
    payload = _extract_json(text)
    try:
        plan = ReportPlan.model_validate(payload)
    except ValidationError as exc:
        raise ReportPlanParseError(f"JSON did not match the contract: {exc}") from exc
    # The model leaves ``databases`` optional so old stored plans still parse
    # (see ``PlannedSection``), which makes this the boundary that requires it of
    # the LLM. A section with no database is not executable, and unlike a draft
    # there is nobody who meant to leave it blank.
    for section in plan.sections:
        if section.source == "database" and not section.databases:
            raise ReportPlanParseError(
                f'Section "{section.title}" uses source "database" but names no '
                'database. Put at least one of the database ids in "databases".'
            )
        # Same boundary, same reason (#204): a merged section with no agreed
        # shape is not executable, and unlike a draft there is nobody who meant
        # to leave it blank.
        if _uncontracted(section):
            raise ReportPlanParseError(f'Section "{section.title}" {_contract_required(section)}')
    check_sources_available(
        plan.sections, connection_ids=connection_ids, has_documents=has_documents
    )
    return plan


async def generate_report_plan(
    provider: str,
    api_key: str,
    model: str,
    *,
    base_url: str | None,
    user_prompt: str,
    databases: Sequence[PlannerDatabase],
    has_documents: bool,
    feedback: list[str],
) -> ReportPlan:
    """One planning attempt: prompt → provider → parsed, availability-checked plan."""
    prompt = build_plan_prompt(
        user_prompt,
        databases=databases,
        has_documents=has_documents,
        feedback=feedback,
    )
    reply = await generate_text(provider, api_key, model, prompt=prompt, base_url=base_url)
    return parse_report_plan(
        reply,
        connection_ids=[database.id for database in databases],
        has_documents=has_documents,
    )


def _draft_json(draft: ReportPlanDraft) -> str:
    """The user's plan as the model sees it — blanks left as empty strings."""
    return json.dumps(draft.model_dump(), ensure_ascii=False, indent=2)


def build_replan_prompt(
    user_prompt: str,
    *,
    draft: ReportPlanDraft,
    databases: Sequence[PlannerDatabase],
    has_documents: bool,
    feedback: list[str],
) -> str:
    """Ask the model to fill the user's blanks and change nothing else.

    The instruction is belt-and-braces — ``merge_draft`` discards any change to a
    supplied field regardless — but a model that follows it wastes fewer rounds.
    """
    feedback_block = ""
    if feedback:
        joined = "\n".join(f"- {f}" for f in feedback)
        feedback_block = (
            f"\nYour previous attempt(s) were rejected. Fix these problems:\n{joined}\n"
        )
    return (
        "A user has edited the plan for a report. Complete it.\n\n"
        "Available sources:\n" + _sources_block(databases, has_documents=has_documents) + "\n\n"
        "The user's plan (empty strings are what they want you to fill in):\n"
        f"{_draft_json(draft)}\n\n"
        "Rules:\n"
        "- Output ONLY a JSON object in the same shape, no prose, no code fences.\n"
        f"- Return EXACTLY {len(draft.sections)} sections, in the same order. Never add "
        "a section, never remove one, never reorder them.\n"
        "- Fill every empty string with a sensible value.\n"
        "- Copy every non-empty value through UNCHANGED, character for character. Do "
        "not improve, translate, or rephrase the user's wording.\n"
        '- Choose a "source" only where it is empty, from the available sources '
        "above. Copy every non-empty one through unchanged.\n"
        '- Fill "databases" only where it is an empty list on a "database" section: '
        "put the database ids that can answer that sub-question in it — one normally, "
        f"at most {MAX_DATABASES_PER_SECTION} if the figures live in different "
        'databases. A "documents" section keeps an empty list. Copy every list that '
        "already has an id through unchanged.\n"
        '- "columns" is the output contract for a section that reads SEVERAL '
        "databases: the columns every one of its queries returns, the same columns in "
        "the same order, so their rows can be put in one table. Fill it only where a "
        "section names more than one database and the list is empty; leave it empty "
        "for every other section, and never use the name "
        f'"{PROVENANCE_COLUMN}".\n'
        "- A sub_question must be self-contained: one source must be able to answer it "
        "on its own.\n"
        "- Write anything you fill in in the language of the surrounding plan.\n"
        f"{feedback_block}\n"
        f"The report the user originally asked for: {user_prompt}\n"
    )


def parse_replan(
    text: str, *, draft: ReportPlanDraft, connection_ids: Collection[str], has_documents: bool
) -> ReportPlan:
    """Parse the reply, merge it under the user's draft, and validate the result.

    The section-count contract is enforced once, in :func:`merge_draft` — this
    function does not repeat the check.
    """
    payload = _extract_json(text)
    try:
        reply = ReportPlanDraft.model_validate(payload)
    except ValidationError as exc:
        raise ReportPlanParseError(f"JSON did not match the contract: {exc}") from exc
    merged = merge_draft(draft, reply)
    return finalize_draft(merged, connection_ids=connection_ids, has_documents=has_documents)


async def generate_replan(
    provider: str,
    api_key: str,
    model: str,
    *,
    base_url: str | None,
    user_prompt: str,
    draft: ReportPlanDraft,
    databases: Sequence[PlannerDatabase],
    has_documents: bool,
    feedback: list[str],
) -> ReportPlan:
    """One re-plan attempt: prompt → provider → merged, validated plan."""
    prompt = build_replan_prompt(
        user_prompt,
        draft=draft,
        databases=databases,
        has_documents=has_documents,
        feedback=feedback,
    )
    reply = await generate_text(provider, api_key, model, prompt=prompt, base_url=base_url)
    return parse_replan(
        reply,
        draft=draft,
        connection_ids=[database.id for database in databases],
        has_documents=has_documents,
    )
