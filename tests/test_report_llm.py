"""Prompt construction and strict JSON parsing for report-query generation."""

import datetime
import json

import pytest

import contextvault.services.report_llm as report_llm
from contextvault.models import DatabaseType
from contextvault.services.report_llm import (
    ReportQueryParseError,
    build_prompt,
    generate_report_query,
    parse_report_query,
)

SCHEMA = [
    {
        "table": "orders",
        "description": "sales orders",
        "columns": [{"name": "city", "description": "customer city"}],
    }
]


def test_prompt_contains_schema_descriptions_dialect_and_feedback() -> None:
    prompt = build_prompt(
        "report for Kyiv",
        exposed_schema=SCHEMA,
        db_type=DatabaseType.MYSQL,
        today=datetime.date(2026, 7, 23),
        max_rows=10_000,
        feedback=["Column not allowed: secret."],
    )
    assert "orders" in prompt and "customer city" in prompt
    assert "mysql" in prompt.lower()
    assert "2026-07-23" in prompt
    assert "Column not allowed: secret." in prompt
    assert "report for Kyiv" in prompt


def test_parse_accepts_plain_and_fenced_json() -> None:
    payload = {
        "sql": "SELECT city FROM orders",
        "chart": {"chart_type": "bar", "x_column": "city", "y_column": None, "title": "Кількість"},
    }
    for text in (json.dumps(payload), f"```json\n{json.dumps(payload)}\n```"):
        query = parse_report_query(text)
        assert query.sql == "SELECT city FROM orders"
        assert query.chart.chart_type == "bar"
        assert query.chart.title == "Кількість"


def test_parse_rejects_prose_bad_chart_type_and_missing_sql() -> None:
    for bad in (
        "no json here",
        '{"chart": {"chart_type": "bar", "title": "t"}}',
        '{"sql": "SELECT 1", "chart": {"chart_type": "hologram", "title": "t"}}',
    ):
        with pytest.raises(ReportQueryParseError):
            parse_report_query(bad)


async def test_generate_calls_textgen_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_generate_text(
        provider: str, api_key: str, model: str, *, prompt: str, base_url: str | None = None
    ) -> str:
        assert provider == "gemini" and "orders" in prompt
        return (
            '{"sql": "SELECT city FROM orders", "chart": {"chart_type": "none", '
            '"x_column": null, "y_column": null, "title": "t"}}'
        )

    monkeypatch.setattr(report_llm, "generate_text", fake_generate_text)
    query = await generate_report_query(
        "gemini",
        "k",
        "m",
        base_url=None,
        user_prompt="p",
        exposed_schema=SCHEMA,
        db_type=DatabaseType.POSTGRES,
        feedback=[],
    )
    assert query.sql == "SELECT city FROM orders"


def _prompt(**overrides: object) -> str:
    kwargs: dict[str, object] = {
        "exposed_schema": SCHEMA,
        "db_type": DatabaseType.POSTGRES,
        "today": datetime.date(2026, 8, 10),
        "max_rows": 10_000,
        "feedback": [],
    }
    kwargs.update(overrides)
    return build_prompt("виручка", **kwargs)  # type: ignore[arg-type]


def test_the_prompt_carries_a_period_note_when_one_was_measured() -> None:
    """The measured note is the whole point: the schema block names columns but
    can never say that a 10-K files quarterly, YTD and annual facts for one tag."""
    prompt = _prompt(period_notes={"orders": "period lengths present: 90d (2479 rows)"})
    assert "period lengths present: 90d (2479 rows)" in prompt


def test_the_prompt_is_unchanged_when_nothing_was_measured() -> None:
    """A probe that failed must leave the prompt byte-identical — the hint is an
    optimisation, never a dependency."""
    assert _prompt(period_notes={}) == _prompt()
    assert _prompt(period_notes=None) == _prompt()


def test_a_note_for_a_table_that_is_not_exposed_reaches_nothing() -> None:
    assert _prompt(period_notes={"not_a_table": "period lengths present: 90d"}) == _prompt()


# --------------------------------------------------------------------------- #
# #202 — a connection describes itself
# --------------------------------------------------------------------------- #


def test_the_description_prompt_shows_the_allow_list_and_asks_for_one_line() -> None:
    """The description describes the ALLOW-LIST, not the live database.

    ``exposed_schema`` is what the planner is shown in #203, so a description
    written from anything else would describe a database the planner cannot
    query. It reuses ``_schema_block``, the same rendering the query prompt uses,
    rather than a second format that could drift from it.
    """
    prompt = report_llm.build_description_prompt(SCHEMA, db_type=DatabaseType.POSTGRES)
    assert "orders" in prompt
    assert "sales orders" in prompt
    # The length cap is stated to the model, not only enforced after the fact.
    assert str(report_llm.MAX_DESCRIPTION_CHARS) in prompt
    assert report_llm._schema_block(SCHEMA) in prompt


def test_a_description_is_trimmed_to_one_line_and_capped() -> None:
    """Models pad. A description is stored, shown in a picker and pasted into a
    prompt, so it is normalised on the way in rather than everywhere it is read."""
    raw = '  "Apple Inc. financials.\n\n  Quarterly and annual revenue."  '
    parsed = report_llm.parse_description(raw)
    assert "\n" not in parsed
    assert not parsed.startswith('"')
    assert parsed == "Apple Inc. financials. Quarterly and annual revenue."

    assert len(report_llm.parse_description("x" * 900)) == report_llm.MAX_DESCRIPTION_CHARS


def test_an_empty_description_is_refused_rather_than_stored() -> None:
    """A blank answer is a failed generation, not a description.

    Storing it would look like success in the panel and would hand #203 an empty
    string to route on.
    """
    with pytest.raises(report_llm.DescriptionUnavailable):
        report_llm.parse_description("   \n  ")


def test_the_schema_fingerprint_ignores_key_order_but_not_content() -> None:
    """Stale means "the allow-list changed", not "the JSON was rewritten".

    A fingerprint that moved when SQLAlchemy or the client happened to serialise
    the same allow-list differently would mark every description stale for no
    reason, and an admin who learns to ignore the marker has lost it.
    """
    one = [{"table": "orders", "description": "d", "columns": [{"name": "id", "description": ""}]}]
    reordered = [
        {"columns": [{"description": "", "name": "id"}], "description": "d", "table": "orders"}
    ]
    assert report_llm.schema_fingerprint(one) == report_llm.schema_fingerprint(reordered)

    changed = [
        {"table": "orders", "description": "d", "columns": [{"name": "total", "description": ""}]}
    ]
    assert report_llm.schema_fingerprint(one) != report_llm.schema_fingerprint(changed)
    # A curated-away column and back again is the same allow-list again.
    assert report_llm.schema_fingerprint([]) != report_llm.schema_fingerprint(one)


class TestTheDescriptionsLanguageAndLength:
    """#215: it is read by a person, in the interface they chose."""

    def test_the_prompt_asks_for_the_requested_language(self) -> None:
        uk = report_llm.build_description_prompt(
            SCHEMA, db_type=DatabaseType.POSTGRES, language="uk"
        )
        assert "Ukrainian" in uk
        assert "in English" not in uk

    def test_english_is_still_askable(self) -> None:
        en = report_llm.build_description_prompt(
            SCHEMA, db_type=DatabaseType.POSTGRES, language="en"
        )
        assert "English" in en
        assert "Ukrainian" not in en

    def test_an_unsupported_language_falls_back_rather_than_raising(self) -> None:
        """A caller's locale is untrusted input — `resolve_language` already
        answers "what language is this?" everywhere else, so it answers here."""
        prompt = report_llm.build_description_prompt(
            SCHEMA, db_type=DatabaseType.POSTGRES, language="klingon"
        )
        assert "English" in prompt

    def test_a_regional_locale_still_resolves(self) -> None:
        """Browsers send `uk-UA`, not `uk`."""
        prompt = report_llm.build_description_prompt(
            SCHEMA, db_type=DatabaseType.POSTGRES, language="uk-UA"
        )
        assert "Ukrainian" in prompt

    def test_the_prompt_asks_for_more_than_a_clause(self) -> None:
        """240 characters was barely one clause once it also had to name the
        period and the entities — and this text is what #203 routes on."""
        prompt = report_llm.build_description_prompt(SCHEMA, db_type=DatabaseType.POSTGRES)
        assert report_llm.MAX_DESCRIPTION_CHARS >= 480
        assert str(report_llm.MAX_DESCRIPTION_CHARS) in prompt

    def test_it_is_still_one_line(self) -> None:
        """Longer, not multi-line: it is rendered in one field and interpolated
        into the planner prompt once per connection."""
        assert "\n" not in report_llm.parse_description("first line\nsecond line")
