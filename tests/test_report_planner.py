"""Plan contract: strict JSON parse, section cap, source-availability validation."""

import json
from typing import Any

import pytest

from contextvault.services.report_planner import (
    MAX_SECTIONS,
    PROVENANCE_COLUMN,
    DraftSection,
    PlannerDatabase,
    ReportPlan,
    ReportPlanDraft,
    ReportPlanParseError,
    build_plan_prompt,
    build_replan_prompt,
    check_sources_available,
    draft_has_blanks,
    finalize_draft,
    generate_report_plan,
    merge_draft,
    parse_replan,
    parse_report_plan,
)

SCHEMA = [{"table": "orders", "description": "", "columns": [{"name": "city", "description": ""}]}]
MSFT_SCHEMA = [
    {"table": "msft_revenue", "description": "", "columns": [{"name": "amount", "description": ""}]}
]

DB = "11111111-1111-1111-1111-111111111111"
DB2 = "22222222-2222-2222-2222-222222222222"

APPLE = PlannerDatabase(id=DB, name="Apple", description="Apple filings", exposed_schema=SCHEMA)
MSFT = PlannerDatabase(id=DB2, name="Microsoft", exposed_schema=MSFT_SCHEMA)  # never described


def _plan_json(sections: list[dict[str, Any]]) -> str:
    """Sections as the model returns them.

    A ``database`` section is routed to :data:`DB` unless the caller says
    otherwise, so the tests that are *about* routing set it explicitly and the
    ones that are about something else stay about that.
    """
    import json

    filled = [
        {**section, "databases": [DB]}
        if section["source"] == "database" and "databases" not in section
        else section
        for section in sections
    ]
    return json.dumps({"sections": filled})


def test_parse_valid_two_source_plan() -> None:
    text = _plan_json(
        [
            {"title": "Дохід", "source": "database", "sub_question": "дохід за Q1"},
            {"title": "Політика", "source": "documents", "sub_question": "правила відпусток"},
        ]
    )
    plan = parse_report_plan(text, connection_ids={DB}, has_documents=True)
    assert [s.source for s in plan.sections] == ["database", "documents"]


def test_parse_tolerates_prose_around_json() -> None:
    text = "Here is the plan:\n" + _plan_json(
        [{"title": "A", "source": "documents", "sub_question": "q"}]
    )
    plan = parse_report_plan(text, connection_ids=set(), has_documents=True)
    assert plan.sections[0].title == "A"


def test_parse_rejects_non_json_and_empty_and_overlong() -> None:
    with pytest.raises(ReportPlanParseError):
        parse_report_plan("no json here", connection_ids={DB}, has_documents=True)
    with pytest.raises(ReportPlanParseError):
        parse_report_plan(_plan_json([]), connection_ids={DB}, has_documents=True)
    too_many = [
        {"title": f"S{i}", "source": "documents", "sub_question": "q"}
        for i in range(MAX_SECTIONS + 1)
    ]
    with pytest.raises(ReportPlanParseError):
        parse_report_plan(_plan_json(too_many), connection_ids={DB}, has_documents=True)


def test_parse_rejects_unavailable_sources() -> None:
    db_only = _plan_json([{"title": "A", "source": "database", "sub_question": "q"}])
    with pytest.raises(ReportPlanParseError, match="no database"):
        parse_report_plan(db_only, connection_ids=set(), has_documents=True)
    docs_only = _plan_json([{"title": "B", "source": "documents", "sub_question": "q"}])
    with pytest.raises(ReportPlanParseError, match="no documents"):
        parse_report_plan(docs_only, connection_ids={DB}, has_documents=False)


def test_prompt_lists_only_available_sources_and_feedback() -> None:
    both = build_plan_prompt("звіт", databases=[APPLE], has_documents=True, feedback=[])
    assert '"database"' in both and '"documents"' in both and "orders" in both
    docs = build_plan_prompt("звіт", databases=[], has_documents=True, feedback=["bad"])
    # The *offer* is what must disappear, not the word: since #203 the rules
    # section explains routing and names "database" whether or not one exists.
    assert '- "database": read-only SQL databases' not in docs
    assert "orders" not in docs
    assert "bad" in docs


async def test_generate_report_plan_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextvault.services.report_planner as planner_module

    async def fake_generate(provider: str, api_key: str, model: str, **kwargs: Any) -> str:
        assert provider == "gemini" and model == "gem-1"
        return _plan_json([{"title": "A", "source": "documents", "sub_question": "q"}])

    monkeypatch.setattr(planner_module, "generate_text", fake_generate)
    plan = await generate_report_plan(
        "gemini",
        "key",
        "gem-1",
        base_url=None,
        user_prompt="звіт",
        databases=[],
        has_documents=True,
        feedback=[],
    )
    assert plan.sections[0].sub_question == "q"


def test_plan_carries_title_and_language() -> None:
    plan = parse_report_plan(
        '{"title": "Аналіз активів", "language": "uk", "sections": '
        '[{"title": "Активи", "source": "database", "databases": ["' + DB + '"], '
        '"sub_question": "які активи?"}]}',
        connection_ids={DB},
        has_documents=False,
    )
    assert plan.title == "Аналіз активів"
    assert plan.language == "uk"


def test_plan_without_title_or_language_still_parses() -> None:
    """Older/looser model replies must not fail a report — the caller falls back."""
    plan = parse_report_plan(
        '{"sections": [{"title": "A", "source": "database", "databases": ["'
        + DB
        + '"], "sub_question": "q"}]}',
        connection_ids={DB},
        has_documents=False,
    )
    assert plan.title == ""
    assert plan.language == ""


def _draft(**over: Any) -> ReportPlanDraft:
    base: dict[str, Any] = {
        "title": "Дохід Apple",
        "language": "uk",
        "sections": [
            DraftSection(
                title="Динаміка",
                source="database",
                databases=[DB],
                sub_question="дохід за FY25?",
            )
        ],
    }
    base.update(over)
    return ReportPlanDraft(**base)


def test_merge_keeps_user_text_when_model_rewrites_it() -> None:
    """The lock: a model that paraphrases the user's sub-question changes nothing."""
    draft = _draft()
    reply = _draft(
        title="Revenue of Apple",
        sections=[
            DraftSection(
                title="Revenue trend", source="documents", sub_question="What was FY25 revenue?"
            )
        ],
    )
    merged = merge_draft(draft, reply)
    assert merged.title == "Дохід Apple"
    assert merged.sections[0].title == "Динаміка"
    assert merged.sections[0].sub_question == "дохід за FY25?"
    # A source the plan already chose is frozen; only a blank one is the model's.
    assert merged.sections[0].source == "database"


def test_merge_fills_a_blank_source_from_the_model() -> None:
    draft = _draft(sections=[DraftSection(title="Маржа", source="", sub_question="Яка маржа?")])
    reply = _draft(sections=[DraftSection(title="X", source="documents", sub_question="Y")])
    assert merge_draft(draft, reply).sections[0].source == "documents"


def test_merge_freezes_a_source_the_plan_already_chose() -> None:
    """The other side of the same guard: once a section has a source, a later round
    cannot move it. Convergence is a property of this function, not of the prompt."""
    draft = _draft(
        sections=[DraftSection(title="Маржа", source="database", sub_question="Яка маржа?")]
    )
    reply = _draft(sections=[DraftSection(title="X", source="documents", sub_question="Y")])
    assert merge_draft(draft, reply).sections[0].source == "database"


def test_merge_fills_only_blanks() -> None:
    draft = _draft(
        title="",
        sections=[
            DraftSection(title="Динаміка", source="database", sub_question="дохід за FY25?"),
            DraftSection(title="Маржа", source="database", sub_question=""),
        ],
    )
    reply = _draft(
        title="Фінансовий огляд",
        sections=[
            DraftSection(title="Ignored", source="documents", sub_question="Ignored too"),
            DraftSection(title="Also ignored", source="documents", sub_question="Яка маржа?"),
        ],
    )
    merged = merge_draft(draft, reply)
    assert merged.title == "Фінансовий огляд"  # was blank → model's
    assert merged.sections[0].sub_question == "дохід за FY25?"  # was set → user's
    assert merged.sections[1].title == "Маржа"  # was set → user's
    assert merged.sections[1].sub_question == "Яка маржа?"  # was blank → model's


def test_merge_treats_whitespace_as_blank() -> None:
    draft = _draft(sections=[DraftSection(title="Маржа", source="database", sub_question="   ")])
    reply = _draft(sections=[DraftSection(title="X", source="database", sub_question="Яка маржа?")])
    assert merge_draft(draft, reply).sections[0].sub_question == "Яка маржа?"


def test_merge_rejects_a_section_count_mismatch() -> None:
    """The reply's section count is model-controlled — the likeliest adversarial input."""
    draft = _draft()
    reply = _draft(
        sections=[
            DraftSection(title="A", source="documents", sub_question="a?"),
            DraftSection(title="B", source="documents", sub_question="b?"),
        ]
    )
    with pytest.raises(ReportPlanParseError, match="same sections"):
        merge_draft(draft, reply)


def test_merge_language_is_locked_like_any_other_field() -> None:
    draft = _draft(language="uk")
    reply = _draft(language="en")
    assert merge_draft(draft, reply).language == "uk"  # user-set → survives

    draft_blank = _draft(language="")
    reply_en = _draft(language="en")
    assert merge_draft(draft_blank, reply_en).language == "en"  # blank → model's


def test_merge_is_idempotent_across_rounds() -> None:
    """The literal statement of "the loop converges": every field the user supplied
    in round one still wins after a second round, no matter what either reply says."""
    draft = _draft(
        title="",
        sections=[
            DraftSection(title="Динаміка", source="database", sub_question="дохід за FY25?"),
            DraftSection(title="", source="database", sub_question=""),
        ],
    )
    reply1 = _draft(
        title="Round one title",
        sections=[
            DraftSection(title="Ignored", source="documents", sub_question="Ignored"),
            DraftSection(title="Маржа", source="documents", sub_question="Яка маржа?"),
        ],
    )
    reply2 = _draft(
        title="Round two title",
        sections=[
            DraftSection(title="Also ignored", source="documents", sub_question="Also ignored"),
            DraftSection(title="Different", source="documents", sub_question="Different?"),
        ],
    )
    once = merge_draft(draft, reply1)
    twice = merge_draft(once, reply2)
    assert twice.title == "Round one title"  # filled once, then stable
    assert twice.sections[0].title == "Динаміка"  # user-supplied from round one
    assert twice.sections[0].sub_question == "дохід за FY25?"
    assert twice.sections[1].title == "Маржа"  # filled in round one, stable in round two
    assert twice.sections[1].sub_question == "Яка маржа?"


def test_draft_has_blanks_detects_each_fillable_field() -> None:
    assert not draft_has_blanks(_draft())
    assert draft_has_blanks(_draft(title=""))
    assert draft_has_blanks(
        _draft(sections=[DraftSection(title="", source="database", sub_question="q")])
    )
    assert draft_has_blanks(
        _draft(sections=[DraftSection(title="T", source="database", sub_question="  ")])
    )
    # A blank language is NOT a blank: resolve_language falls back to the prompt's
    # script, so the model is never asked to fill it.
    assert not draft_has_blanks(_draft(language=""))


def test_draft_has_blanks_counts_a_missing_source() -> None:
    """A section the reader added has no source until the model chooses one, so it
    is a blank like any other — the thing that makes a re-plan available."""
    assert draft_has_blanks(_draft(sections=[DraftSection(title="T", source="", sub_question="q")]))


def test_a_draft_section_defaults_to_no_source() -> None:
    assert DraftSection(title="T", sub_question="q").source == ""


def test_finalize_rejects_blanks_naming_the_section() -> None:
    with pytest.raises(ReportPlanParseError, match=r"^The report needs a title\.$"):
        finalize_draft(_draft(title=""), connection_ids={DB}, has_documents=True)
    blank_q = _draft(sections=[DraftSection(title="Маржа", source="database", sub_question="")])
    with pytest.raises(ReportPlanParseError, match='Section 1 \\("Маржа"\\)'):
        finalize_draft(blank_q, connection_ids={DB}, has_documents=True)


def test_finalize_rejects_a_section_with_no_source() -> None:
    """A blank source reaching approval is a contract error, and must arrive as a
    ReportPlanParseError — that is the self-repair feedback channel. A raw pydantic
    ValidationError would crash the caller instead of feeding the repair loop."""
    blank_source = _draft(
        sections=[DraftSection(title="Маржа", source="", sub_question="Яка маржа?")]
    )
    with pytest.raises(ReportPlanParseError, match='Section 1 \\("Маржа"\\).*source'):
        finalize_draft(blank_source, connection_ids={DB}, has_documents=True)


def test_finalize_rejects_unavailable_source() -> None:
    with pytest.raises(ReportPlanParseError, match="no database"):
        finalize_draft(_draft(), connection_ids=set(), has_documents=True)


def test_check_sources_available_lets_a_blank_source_through() -> None:
    """A reader-added section has no source yet, and `/replan` is what fills it — so
    the availability check must ignore a blank rather than reject it. `finalize_draft`
    is what refuses a still-blank source at approval."""
    check_sources_available(
        [DraftSection(title="T", source="", sub_question="q")],
        connection_ids=set(),
        has_documents=False,
    )


def test_finalize_returns_executable_plan() -> None:
    plan = finalize_draft(_draft(), connection_ids={DB}, has_documents=True)
    assert isinstance(plan, ReportPlan)
    assert plan.sections[0].sub_question == "дохід за FY25?"


def test_replan_prompt_shows_the_draft_and_the_rules() -> None:
    draft = _draft(
        sections=[
            DraftSection(title="Динаміка", source="database", sub_question="дохід за FY25?"),
            DraftSection(title="Маржа", source="database", sub_question=""),
        ]
    )
    prompt = build_replan_prompt(
        "звіт", draft=draft, databases=[APPLE], has_documents=True, feedback=[]
    )
    assert "Динаміка" in prompt and "Маржа" in prompt
    assert "2" in prompt  # the required section count
    assert "orders" in prompt  # the schema block is still offered


def test_replan_prompt_asks_for_a_missing_source_and_locks_the_rest() -> None:
    prompt = build_replan_prompt(
        "Дохід",
        draft=_draft(sections=[DraftSection(title="Маржа", source="", sub_question="")]),
        databases=[APPLE],
        has_documents=True,
        feedback=[],
    )
    assert "Never change any" not in prompt
    assert 'Choose a "source" only where it is empty' in prompt


def test_parse_replan_fills_a_blank_source_end_to_end() -> None:
    draft = _draft(sections=[DraftSection(title="Маржа", source="", sub_question="")])
    reply = json.dumps(
        {
            "title": "Дохід Apple",
            "language": "uk",
            "sections": [{"title": "Маржа", "source": "documents", "sub_question": "Яка маржа?"}],
        }
    )
    plan = parse_replan(reply, draft=draft, connection_ids={DB}, has_documents=True)
    assert plan.sections[0].source == "documents"
    assert plan.sections[0].sub_question == "Яка маржа?"


def test_parse_replan_rejects_a_changed_section_count() -> None:
    """A model that re-adds the section the user deleted violates the contract."""
    draft = _draft()
    reply = json.dumps(
        {
            "title": "Дохід Apple",
            "language": "uk",
            "sections": [
                {"title": "Динаміка", "source": "database", "sub_question": "дохід за FY25?"},
                {"title": "Сезонність", "source": "database", "sub_question": "по кварталах?"},
            ],
        }
    )
    with pytest.raises(ReportPlanParseError, match="same sections"):
        parse_replan(reply, draft=draft, connection_ids={DB}, has_documents=True)


def test_parse_replan_merges_and_returns_an_executable_plan() -> None:
    draft = _draft(
        sections=[DraftSection(title="Маржа", source="database", databases=[DB], sub_question="")]
    )
    reply = json.dumps(
        {
            "title": "Rewritten title",
            "language": "en",
            "sections": [
                {"title": "Rewritten", "source": "documents", "sub_question": "Яка маржа?"}
            ],
        }
    )
    plan = parse_replan(reply, draft=draft, connection_ids={DB}, has_documents=True)
    assert plan.title == "Дохід Apple"  # user's, not the model's
    assert plan.language == "uk"  # user's
    assert plan.sections[0].title == "Маржа"  # user's
    assert plan.sections[0].source == "database"  # user's
    assert plan.sections[0].sub_question == "Яка маржа?"  # was blank → model's


def test_parse_replan_rejects_non_json() -> None:
    with pytest.raises(ReportPlanParseError):
        parse_replan("nope", draft=_draft(), connection_ids={DB}, has_documents=True)


class TestComparisonPlanning:
    """A 100-report run answered "compare Apple with Microsoft" with four sections.

    "Apple ROA", "Apple ROE", "Microsoft ROA", "Microsoft ROE" — parallel
    descriptions that compare nothing and leave the reader to do the work.
    """

    def test_the_prompt_forbids_one_section_per_entity(self) -> None:
        prompt = build_plan_prompt(
            "Порівняй рентабельність Apple та Microsoft",
            databases=[APPLE],
            has_documents=False,
            feedback=[],
        )
        assert "ONE section that asks for them TOGETHER" in prompt
        assert "Do NOT write one section per entity" in prompt

    def test_the_prompt_warns_against_guessing_literal_values(self) -> None:
        """Guessed literals produced eleven reports that matched nothing at all."""
        prompt = build_plan_prompt(
            "Скільки податків сплатила Microsoft?",
            databases=[APPLE],
            has_documents=False,
            feedback=[],
        )
        assert "unsure of a code, label or category name" in prompt


class TestSectionRouting:
    """#203: a section names WHICH database it reads, not just that it reads one.

    A repository owns several connections since #201, so ``source="database"`` —
    a *kind* of source — stopped being enough to execute a section.
    """

    def test_a_database_section_names_the_database_it_reads(self) -> None:
        text = _plan_json(
            [{"title": "Дохід", "source": "database", "databases": [DB2], "sub_question": "q"}]
        )
        plan = parse_report_plan(text, connection_ids={DB, DB2}, has_documents=False)
        assert plan.sections[0].databases == [DB2]

    def test_rejects_a_database_section_that_names_no_database(self) -> None:
        text = _plan_json(
            [{"title": "Дохід", "source": "database", "databases": [], "sub_question": "q"}]
        )
        with pytest.raises(ReportPlanParseError, match="names no database"):
            parse_report_plan(text, connection_ids={DB}, has_documents=False)

    def test_rejects_a_database_that_belongs_to_another_repository(self) -> None:
        """The security rule, not a typo guard: `/replan` and `/approve` take an
        untrusted draft, so an id from outside this repository must not route a
        section at someone else's database — #201's resolve-by-pair lesson."""
        text = _plan_json(
            [{"title": "Дохід", "source": "database", "databases": [DB2], "sub_question": "q"}]
        )
        with pytest.raises(ReportPlanParseError, match="does not belong to this repository"):
            parse_report_plan(text, connection_ids={DB}, has_documents=False)

    def test_rejects_a_documents_section_that_names_a_database(self) -> None:
        text = _plan_json(
            [{"title": "Політика", "source": "documents", "databases": [DB], "sub_question": "q"}]
        )
        with pytest.raises(ReportPlanParseError, match="must not name a database"):
            parse_report_plan(text, connection_ids={DB}, has_documents=True)

    def test_the_prompt_offers_every_database_with_its_id_and_schema(self) -> None:
        prompt = build_plan_prompt(
            "Порівняй Apple та Microsoft", databases=[APPLE, MSFT], has_documents=False, feedback=[]
        )
        assert DB in prompt and DB2 in prompt
        assert "Apple" in prompt and "Microsoft" in prompt
        assert "Apple filings" in prompt  # the #202 description
        assert "orders" in prompt and "msft_revenue" in prompt  # both allow-lists

    def test_a_database_nobody_described_is_still_offered(self) -> None:
        """#202's counterpart rule. A description only exists after introspect +
        curate, so gating on one would make curating a database the thing that
        silently removes it from every report."""
        prompt = build_plan_prompt("звіт", databases=[MSFT], has_documents=False, feedback=[])
        assert DB2 in prompt and "Microsoft" in prompt and "msft_revenue" in prompt

    def test_merge_freezes_a_routing_the_plan_already_chose(self) -> None:
        draft = _draft(
            sections=[
                DraftSection(title="Динаміка", source="database", databases=[DB], sub_question="q")
            ]
        )
        reply = _draft(
            sections=[DraftSection(title="X", source="database", databases=[DB2], sub_question="Y")]
        )
        assert merge_draft(draft, reply).sections[0].databases == [DB]

    def test_merge_fills_a_blank_routing_from_the_model(self) -> None:
        draft = _draft(
            sections=[DraftSection(title="Динаміка", source="database", sub_question="q")]
        )
        reply = _draft(
            sections=[DraftSection(title="X", source="database", databases=[DB2], sub_question="Y")]
        )
        assert merge_draft(draft, reply).sections[0].databases == [DB2]

    def test_an_unrouted_database_section_is_a_blank_to_fill(self) -> None:
        assert draft_has_blanks(
            _draft(sections=[DraftSection(title="T", source="database", sub_question="q")])
        )

    def test_an_unrouted_documents_section_is_not_a_blank(self) -> None:
        """Only a database section owes a database — asking a documents section
        which one it reads would put every plan into a re-plan loop it cannot exit."""
        assert not draft_has_blanks(
            _draft(sections=[DraftSection(title="T", source="documents", sub_question="q")])
        )

    def test_the_availability_check_lets_an_unrouted_draft_through(self) -> None:
        """Mid-edit, an empty routing is a blank the next re-plan fills — exactly
        like a blank source. `finalize_draft` is what refuses one at approval."""
        check_sources_available(
            [DraftSection(title="T", source="database", sub_question="q")],
            connection_ids={DB},
            has_documents=False,
        )

    def test_finalize_refuses_an_unrouted_section_naming_it(self) -> None:
        unrouted = _draft(
            sections=[DraftSection(title="Маржа", source="database", sub_question="Яка маржа?")]
        )
        with pytest.raises(ReportPlanParseError, match='Section 1 \\("Маржа"\\).*database to read'):
            finalize_draft(unrouted, connection_ids={DB}, has_documents=True)


class TestPlansStoredBeforeRoutingExisted:
    """Every plan written before #203 lacks the ``databases`` key entirely.

    ``ReportPlan`` is not only the LLM's contract — it also parses plans back out
    of the ``generated_reports.plan`` JSONB column on the way to the reader. A
    required field would therefore have turned "list my reports" into a 500 for
    every report that already existed.
    """

    OLD = {
        "title": "Аналіз",
        "language": "uk",
        "sections": [{"title": "Дохід", "source": "database", "sub_question": "дохід?"}],
    }

    def test_an_old_stored_plan_still_parses_for_display(self) -> None:
        plan = ReportPlan.model_validate(self.OLD)
        assert plan.sections[0].source == "database"
        assert plan.sections[0].databases == []  # unrouted, not invalid

    def test_the_llm_is_still_required_to_route(self) -> None:
        """The other half: leniency is for stored data, not for the model. The
        rule moved to the trust boundary rather than being dropped."""
        with pytest.raises(ReportPlanParseError, match="names no database"):
            parse_report_plan(json.dumps(self.OLD), connection_ids={DB}, has_documents=False)

    def test_an_old_stored_plan_carries_no_output_contract(self) -> None:
        """#204 adds ``columns`` to the same persisted model, so it inherits the
        same rule: default, never required, or every stored report 500s on read."""
        assert ReportPlan.model_validate(self.OLD).sections[0].columns == []


CONTRACT = ["company", "fiscal_year", "revenue"]


def _merged(**overrides: Any) -> dict[str, Any]:
    """A section that reads both databases and merges them."""
    return {
        "title": "Порівняння",
        "source": "database",
        "databases": [DB, DB2],
        "columns": CONTRACT,
        "sub_question": "дохід Apple та Microsoft за роками",
        **overrides,
    }


class TestSectionsThatMergeSeveralDatabases:
    """#204: a section reads up to three databases and merges their rows.

    Two databases cannot be joined by one SQL query, so the section names an
    output contract — the SELECT list every one of its queries must return — and
    the executor concatenates the conforming results.
    """

    def test_a_section_may_name_two_databases_when_it_declares_a_contract(self) -> None:
        plan = parse_report_plan(
            _plan_json([_merged()]), connection_ids={DB, DB2}, has_documents=False
        )
        assert plan.sections[0].databases == [DB, DB2]
        assert plan.sections[0].columns == CONTRACT

    def test_rejects_more_databases_than_one_section_may_merge(self) -> None:
        """The ceiling is latency and spend — each database is a full NL→SQL
        self-repair loop — so it is a number, not an absence of one."""
        third = "33333333-3333-3333-3333-333333333333"
        fourth = "44444444-4444-4444-4444-444444444444"
        text = _plan_json([_merged(databases=[DB, DB2, third, fourth])])
        with pytest.raises(ReportPlanParseError, match="at most 3 databases"):
            parse_report_plan(text, connection_ids={DB, DB2, third, fourth}, has_documents=False)

    def test_rejects_two_databases_with_no_output_contract(self) -> None:
        """Without one there is nothing to concatenate on: two databases rarely
        spell the same thing the same way, and this repo owns the proof — the
        production replica renamed `trade_date` to `date`."""
        text = _plan_json([_merged(columns=[])])
        with pytest.raises(ReportPlanParseError, match="must list the columns"):
            parse_report_plan(text, connection_ids={DB, DB2}, has_documents=False)

    def test_the_message_for_a_missing_contract_tells_the_model_what_to_do(self) -> None:
        """It is self-repair feedback, so it must carry the fix, not the refusal."""
        text = _plan_json([_merged(columns=[])])
        with pytest.raises(ReportPlanParseError, match="same columns, in the same order"):
            parse_report_plan(text, connection_ids={DB, DB2}, has_documents=False)

    def test_the_availability_check_lets_an_uncontracted_draft_through(self) -> None:
        """Symmetry with #203's unrouted draft, and it is load-bearing: `/replan`
        runs this check on the user's own draft, so rejecting a blank contract
        here would 400 the very reader who added the second database and asked
        the model to work out the shape."""
        check_sources_available(
            [DraftSection(**_merged(columns=[]))],
            connection_ids={DB, DB2},
            has_documents=False,
        )

    def test_a_single_database_section_still_needs_no_contract(self) -> None:
        """The overwhelmingly common section is unchanged — and so is every plan
        already stored, which is why no migration ships with this card."""
        plan = parse_report_plan(
            _plan_json([{"title": "Дохід", "source": "database", "sub_question": "q"}]),
            connection_ids={DB},
            has_documents=False,
        )
        assert plan.sections[0].columns == []

    def test_rejects_a_documents_section_that_declares_a_contract(self) -> None:
        text = _plan_json(
            [{"title": "Політика", "source": "documents", "columns": CONTRACT, "sub_question": "q"}]
        )
        with pytest.raises(ReportPlanParseError, match="must not list columns"):
            parse_report_plan(text, connection_ids={DB}, has_documents=True)

    def test_rejects_a_contract_that_repeats_a_column(self) -> None:
        """The columns become SQL aliases and the chart names one of them; a
        duplicate makes "the third column" ambiguous in both."""
        text = _plan_json([_merged(columns=["company", "revenue", "company"])])
        with pytest.raises(ReportPlanParseError, match="names the same column twice"):
            parse_report_plan(text, connection_ids={DB, DB2}, has_documents=False)

    def test_rejects_a_contract_that_claims_the_provenance_column(self) -> None:
        """Rejected rather than silently renamed: the merge adds this column, and
        a rename would break the chart spec that names it."""
        text = _plan_json([_merged(columns=["company", PROVENANCE_COLUMN])])
        with pytest.raises(ReportPlanParseError, match="reserved"):
            parse_report_plan(text, connection_ids={DB, DB2}, has_documents=False)

    def test_finalize_refuses_a_merged_section_with_no_contract(self) -> None:
        draft = _draft(sections=[DraftSection(**_merged(columns=[]))])
        with pytest.raises(ReportPlanParseError, match="must list the columns"):
            finalize_draft(draft, connection_ids={DB, DB2}, has_documents=False)

    def test_merge_freezes_a_contract_the_plan_already_chose(self) -> None:
        """#186's user-first lock, applied to the new field too."""
        draft = _draft(sections=[DraftSection(**_merged())])
        reply = _draft(sections=[DraftSection(**_merged(columns=["x", "y"]))])
        assert merge_draft(draft, reply).sections[0].columns == CONTRACT

    def test_merge_fills_a_blank_contract_from_the_model(self) -> None:
        draft = _draft(sections=[DraftSection(**_merged(columns=[]))])
        reply = _draft(sections=[DraftSection(**_merged())])
        assert merge_draft(draft, reply).sections[0].columns == CONTRACT

    def test_a_merged_section_with_no_contract_is_a_blank_to_fill(self) -> None:
        """Same division of labour as an unrouted section: mid-edit it is a blank
        the next re-plan fills, and only `finalize_draft` refuses one."""
        assert draft_has_blanks(_draft(sections=[DraftSection(**_merged(columns=[]))]))

    def test_the_prompt_no_longer_splits_entities_across_databases(self) -> None:
        """#203 added that exception because a section could read only one
        database. It is wrong the moment a section can merge, and leaving it in
        would keep the planner splitting the very comparison this card joins."""
        prompt = build_plan_prompt(
            "Порівняй Apple та Microsoft", databases=[APPLE, MSFT], has_documents=False, feedback=[]
        )
        assert "ONE section that asks for them TOGETHER" in prompt
        assert "one section each" not in prompt

    def test_the_prompt_asks_for_one_merged_section_and_names_the_contract(self) -> None:
        prompt = build_plan_prompt(
            "Порівняй Apple та Microsoft", databases=[APPLE, MSFT], has_documents=False, feedback=[]
        )
        assert '"columns"' in prompt
        assert "same columns, in the same order" in prompt

    def test_the_replan_prompt_explains_the_contract_too(self) -> None:
        """`/replan` is the path a reader's edited plan takes; a model that has
        never been told the rule cannot fill the field it is asked to fill."""
        prompt = build_replan_prompt(
            "звіт",
            draft=_draft(sections=[DraftSection(**_merged(columns=[]))]),
            databases=[APPLE, MSFT],
            has_documents=False,
            feedback=[],
        )
        assert '"columns"' in prompt
