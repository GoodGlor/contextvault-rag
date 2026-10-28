"""The report document as HTML — asserted on markup, never on rendered bytes.

fpdf compressed its content streams, so grepping the PDF passed even when the
defect was present; that trap is recorded in the project's working rules. The
document is built as a string precisely so it is not available here.

The statistics rules (which columns may be totalled, and how a figure is written)
live in ``_numeric_stats`` and ``report_format`` and keep their own tests below —
they moved here with the function, unchanged.
"""

import datetime
import uuid
from decimal import Decimal
from typing import Any

from contextvault.services.report_blocks import Block
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_html import (
    CoverBrand,
    ReportAssetRef,
    _numeric_stats,
    build_report_html,
    stylesheet,
)
from contextvault.services.report_style import TEMPLATES, resolve_style
from contextvault.services.report_tokens import StyleTokens, apply_tokens
from contextvault.services.report_types import SectionResult, SectionSourceRef

RESULT = QueryResult(columns=["city", "revenue"], rows=[("Київ", 120), ("Львів", 80)])


def _data_section(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "title": "Дохід за містами",
        "source": "database",
        "sub_question": "дохід",
        "kind": "data",
        "result": RESULT,
    }
    return SectionResult(**{**base, **overrides})


# --- the new attack surface ---------------------------------------------------


def test_a_cell_value_cannot_inject_markup() -> None:
    """New with this change: fpdf drew text, HTML executes it.

    Cell values come from a customer's database and section prose from an LLM,
    and both are now interpolated into markup.
    """
    hostile = QueryResult(columns=["name"], rows=[("</td></tr><script>alert(1)</script>",)])
    html = build_report_html(
        title="<script>alert('title')</script>",
        sections=[_data_section(result=hostile)],
        language="uk",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_hostile_column_label_cannot_break_out_of_its_attribute() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(column_labels={"city": '"><img src=x onerror=alert(1)>'})],
        language="uk",
    )
    assert "<img" not in html


def test_a_hostile_section_title_cannot_inject_markup() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(title="<style>body{display:none}</style>Розділ")],
        language="uk",
    )
    assert "<style>body" not in html
    assert "Розділ" in html


# --- the two traps the spike found --------------------------------------------


def test_every_row_is_kept_whole_across_a_page_break() -> None:
    """Without this a row straddling a break is cut in half, and the continuation
    page opens with blank cells that read as missing data rather than as a layout
    artefact."""
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "break-inside: avoid" in html


def test_a_wide_table_is_never_silently_clipped() -> None:
    """The default `table-layout: auto` does not shrink a wide table — it lets the
    last columns run off the page edge and cuts them there, with no warning."""
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "table-layout: fixed" in html
    assert "overflow-wrap: anywhere" in html


def test_every_column_of_a_wide_result_reaches_the_page() -> None:
    columns = [f"measure_{index}" for index in range(10)]
    labels = {name: f"Показник {index}" for index, name in enumerate(columns)}
    result = QueryResult(
        columns=["quarter_end", *columns],
        rows=[("2024-03-31", *(Decimal("123456789012") for _ in columns))],
    )
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(result=result, column_labels=labels)],
        language="uk",
    )
    for heading in labels.values():
        assert heading in html, heading


def test_a_very_long_value_stays_inside_its_cell() -> None:
    """The 300-character XBRL description. fpdf needed measuring and ellipsis
    because it painted straight over the neighbouring cell; CSS wraps instead."""
    result = QueryResult(
        columns=["tag", "description", "value"],
        rows=[("us-gaap:Tag1", "Опис показника, який буває дуже довгим " * 8, Decimal("1"))],
    )
    html = build_report_html(title="Звіт", sections=[_data_section(result=result)], language="uk")
    assert "us-gaap:Tag1" in html
    assert "Опис показника" in html


# --- the document ---------------------------------------------------------------


def test_the_chart_is_embedded_as_svg_when_there_is_one() -> None:
    section = _data_section(chart_svg=b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "data:image/svg+xml;base64," in html


def test_a_section_without_a_chart_embeds_no_image() -> None:
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "data:image" not in html


def test_a_long_result_shows_both_of_its_ends_and_says_what_it_omitted() -> None:
    """The first fifty rows of a twenty-year daily series are all 2004."""
    result = QueryResult(
        columns=["day", "value"],
        rows=[(f"2024-{1 + index // 28:02d}-{1 + index % 28:02d}", index) for index in range(60)],
    )
    html = build_report_html(title="Звіт", sections=[_data_section(result=result)], language="uk")
    assert "2024-01-01" in html  # the head
    assert "2024-03-04" in html  # the tail
    assert "посередині" in html  # the omission is stated, not silent


def test_an_empty_result_says_so_instead_of_printing_a_bare_zero() -> None:
    """A campaign run delivered a one-page report whose whole content was
    "Рядків: 0" under a confident heading."""
    empty = QueryResult(columns=["quarter_end", "revenue"], rows=[])
    html = build_report_html(title="Звіт", sections=[_data_section(result=empty)], language="uk")
    assert "немає даних" in html
    assert "Рядків" not in html


def test_a_failed_section_explains_itself_in_the_report_language() -> None:
    """An English sentence used to sit inside an otherwise Ukrainian report."""
    failed = SectionResult(
        title="Зламана секція",
        source="database",
        sub_question="q",
        kind="error",
        error="relation does not exist",
    )
    html = build_report_html(title="Звіт", sections=[failed], language="uk")
    assert "не вдалося сформувати" in html
    assert "could not be generated" not in html


def test_a_failed_section_shows_a_summary_not_the_whole_driver_error() -> None:
    """A rejected query put half a page of SQLAlchemy internals under the heading."""
    raw = (
        "Query `SELECT period_end, company FROM (SELECT ...)` was rejected: Query failed: "
        "(sqlalchemy.dialects.postgresql.asyncpg.ProgrammingError) "
        "<class 'asyncpg.exceptions.AmbiguousColumnError'>: column reference \"tag\" is ambiguous\n"
        "[SQL: SELECT period_end, company, tag_label, value FROM (SELECT period_end, 'Apple' "
        "AS company " + "x" * 900 + " LIMIT 10000]\n"
        "(Background on this error at: https://sqlalche.me/e/20/f405)"
    )
    failed = SectionResult(
        title="Аналіз активів", source="database", sub_question="q", kind="error", error=raw
    )
    html = build_report_html(title="Звіт", sections=[failed], language="uk")
    assert "ambiguous" in html
    assert "[SQL:" not in html
    assert "sqlalche.me" not in html


def test_a_text_section_renders_its_own_numbered_sources() -> None:
    section = SectionResult(
        title="Політика відпусток",
        source="documents",
        sub_question="правила",
        kind="text",
        prose="Відпустка триває 24 дні [1].",
        sources=[SectionSourceRef(number=1, title="policy.pdf", source_id=uuid.uuid4())],
    )
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "Відпустка триває 24 дні [1]." in html
    assert "policy.pdf" in html


def test_the_readers_prompt_is_never_printed() -> None:
    """It used to be rendered as the heading *and* again as body text beneath it."""
    section = _data_section(sub_question="покажи все про конкурентів")
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "покажи все про конкурентів" not in html


def test_a_control_character_never_reaches_the_page() -> None:
    """A model reply carried a bare ESC into a table cell."""
    result = QueryResult(columns=["label"], rows=[("Дохід\x1b Apple",)])
    html = build_report_html(
        title="Звіт\x1b", sections=[_data_section(result=result)], language="uk"
    )
    assert "\x1b" not in html
    assert "Дохід" in html


def test_the_page_footer_is_written_in_the_reports_language() -> None:
    assert "Стор." in build_report_html(title="Звіт", sections=[], language="uk")
    assert "Page" in build_report_html(title="Report", sections=[], language="en")


def test_large_decimal_never_renders_in_scientific_notation() -> None:
    """The "3.6498000E+11" defect."""
    result = QueryResult(columns=["fiscal_year", "value"], rows=[(2024, Decimal("3.6498000E+11"))])
    html = build_report_html(title="Звіт", sections=[_data_section(result=result)], language="uk")
    assert "E+" not in html
    assert "млрд" in html
    assert "2024" in html  # a year keeps its digits, ungrouped and unscaled


# --- statistics rules, moved here with _numeric_stats ---------------------------


def test_bool_column_is_not_treated_as_numeric_stat() -> None:
    result = QueryResult(columns=["city", "active"], rows=[("Kyiv", True), ("Lviv", False)])
    stats = _numeric_stats(result, language="en", column_labels={})
    assert not any("active" in name for name, _ in stats)


def test_year_column_produces_no_totals() -> None:
    """The "Σ fiscal_year: 68,544.00" defect: a year is numeric but not a quantity."""
    result = QueryResult(
        columns=["fiscal_year", "value"],
        rows=[(2024, Decimal("364980000000")), (2023, Decimal("352583000000"))],
    )
    names = [name for name, _ in _numeric_stats(result, language="uk", column_labels={})]
    assert not any("fiscal_year" in name for name in names)
    assert any(name.startswith("Сума") and "value" in name for name in names)


def test_percent_column_gets_a_mean_but_never_a_sum() -> None:
    result = QueryResult(
        columns=["percentage_of_assets"], rows=[(Decimal("84.39"),), (Decimal("82.37"),)]
    )
    names = [name for name, _ in _numeric_stats(result, language="en", column_labels={})]
    assert any(name.startswith("Average") for name in names)
    assert not any(name.startswith("Total") for name in names)


def test_stats_use_localised_labels_and_column_headings() -> None:
    # Two rows, because the total and the mean are deliberately withheld from a
    # single-row result — this test is about the localised label, not that rule.
    result = QueryResult(
        columns=["value"], rows=[(Decimal("1000000000"),), (Decimal("1000000000"),)]
    )
    stats = dict(_numeric_stats(result, language="uk", column_labels={"value": "Дохід"}))
    assert "Рядків" in stats
    assert "Сума Дохід" in stats
    assert "млрд" in stats["Сума Дохід"]


def test_the_statistics_name_themselves_in_words_not_notation() -> None:
    """Σ and x̄ are notation, not words.

    They sit side by side on one line — the single place where telling a total
    from an average matters most — and a reader who has not met the symbols
    cannot. Reported from a generated report whose line read
    ``x̄ Операційний грошовий потік: 42,88 млрд · Σ Капітальні витрати: 345,81 млрд``.
    """
    result = QueryResult(
        columns=["revenue"],
        rows=[(Decimal("1000000000"),), (Decimal("3000000000"),)],
    )
    uk = dict(_numeric_stats(result, language="uk", column_labels={"revenue": "Дохід"}))
    assert "Сума Дохід" in uk, uk
    assert "Середнє Дохід" in uk, uk
    english = dict(_numeric_stats(result, language="en", column_labels={"revenue": "Revenue"}))
    assert "Total Revenue" in english, english
    assert "Average Revenue" in english, english
    # The heading keeps whatever the column is called; only the leading word changes.
    assert not any("Σ" in key or "x̄" in key for key in (*uk, *english))


def test_a_price_column_gets_no_total_in_the_statistics() -> None:
    """Σ over 5,350 daily closes was printed as if it meant something."""
    result = QueryResult(
        columns=["trade_date", "close_price"],
        rows=[(f"2024-01-{day:02d}", Decimal("120.5")) for day in range(1, 10)],
    )
    stats = dict(_numeric_stats(result, language="uk", column_labels={}))
    assert not any(name.startswith("Сума") for name in stats), stats
    assert any(name.startswith("Середнє") for name in stats), stats


def test_a_balance_gets_no_total_across_quarters() -> None:
    result = QueryResult(
        columns=["quarter_end", "total_assets"],
        rows=[(f"2024-0{index}-30", Decimal("352583000000")) for index in range(1, 5)],
    )
    stats = dict(_numeric_stats(result, language="uk", column_labels={}))
    assert not any(name.startswith("Сума") for name in stats), stats


def test_a_single_row_result_gets_no_total_and_no_mean() -> None:
    """A third of the totals a 100-report run printed were over one row."""
    result = QueryResult(columns=["period_end", "total_assets"], rows=[("2025-09-30", 536469)])
    stats = dict(_numeric_stats(result, language="en", column_labels={}))
    assert list(stats) == ["Rows"], stats

    two = QueryResult(
        columns=["period_end", "revenue"], rows=[("2024-09-30", 391), ("2025-09-30", 416)]
    )
    assert any(
        name.startswith("Total") for name, _ in _numeric_stats(two, language="en", column_labels={})
    )


def test_a_return_measure_is_never_totalled() -> None:
    """The campaign printed "Σ ROA" and "Σ ROE" across five fiscal years."""
    result = QueryResult(
        columns=["fiscal_year", "roa", "roe"],
        rows=[(2023, Decimal("28.4"), Decimal("171.9")), (2024, Decimal("25.7"), Decimal("164.6"))],
    )
    names = [name for name, _ in _numeric_stats(result, language="en", column_labels={})]
    assert not any(name.startswith("Total") for name in names), names
    assert any(name.startswith("Average") for name in names), names


# --- the template drives the document -----------------------------------------


def test_the_paper_colour_goes_on_the_page_not_on_the_body() -> None:
    """``html``/``body`` backgrounds paint only the content box.

    Measured against WeasyPrint 69: a dark template styled that way renders a
    near-black block inside a white frame on every page — a defect that looks
    like a broken renderer and passes every assertion about text. Only ``@page``
    paints the margins.
    """
    html = build_report_html(title="Звіт", sections=[], language="uk", style=TEMPLATES["prezydia"])
    page_rule = html[html.index("@page") : html.index("html {")]
    assert "#14161d" in page_rule


def test_each_template_puts_its_own_ink_and_type_on_the_page() -> None:
    for key in ("kvarta", "prezydia", "chasopys"):
        style = TEMPLATES[key]
        html = build_report_html(title="Звіт", sections=[], language="uk", style=style)
        assert style.paper in html, key
        assert style.ink in html, key
        assert style.body_family in html, key


def test_the_serif_template_asks_for_the_serif_family_and_two_columns() -> None:
    html = build_report_html(title="Звіт", sections=[], language="uk", style=TEMPLATES["chasopys"])
    assert "Report Serif" in html
    assert "columns: 2" in html


def test_a_template_that_numbers_its_sections_says_so_in_the_stylesheet() -> None:
    numbered = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", style=TEMPLATES["kvarta"]
    )
    plain = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", style=TEMPLATES["minimum"]
    )
    assert "counter(section-number)" in numbered
    assert "counter(section-number)" not in plain


def test_the_zebra_row_is_a_template_decision_not_a_constant() -> None:
    striped = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", style=TEMPLATES["kvarta"]
    )
    plain = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", style=TEMPLATES["chasopys"]
    )
    assert "nth-child(even)" in striped
    assert "nth-child(even)" not in plain


def test_omitting_the_style_renders_the_default_template() -> None:
    """Every existing caller passes no style and must keep working."""
    assert TEMPLATES["kvarta"].paper in build_report_html(title="Звіт", sections=[], language="uk")


# --- the cover and the running header ------------------------------------------


def test_the_cover_carries_the_title_and_the_date_and_never_the_prompt() -> None:
    """The reader's prompt stays off the page — a step-2 rule the cover inherits.

    It used to be printed as the heading *and* again as body text beneath it. It
    is still on the report row and in the history list, where it belongs.
    """
    html = build_report_html(
        title="Квартальний звіт",
        sections=[],
        language="uk",
        report_date=datetime.date(2026, 8, 4),
    )
    assert 'class="cover"' in html
    assert "Квартальний звіт" in html
    assert "04.08.2026" in html
    assert "Сформовано" in html


def test_a_report_with_no_date_still_renders_a_cover() -> None:
    """A rerender of a report predating the column has no creation date to show."""
    html = build_report_html(title="Звіт", sections=[], language="uk")
    assert 'class="cover"' in html
    assert "Сформовано" not in html


def test_the_cover_words_follow_the_report_language() -> None:
    english = build_report_html(
        title="Report", sections=[], language="en", report_date=datetime.date(2026, 8, 4)
    )
    assert "Prepared" in english
    assert "Сформовано" not in english


def test_the_running_header_is_declared_and_suppressed_on_the_cover() -> None:
    html = build_report_html(title="Звіт", sections=[], language="uk")
    assert "string-set" in html
    assert "string(doctitle)" in html
    assert "@page :first" in html


def test_the_title_is_written_once_so_the_header_cannot_duplicate_it() -> None:
    """The h1 moved onto the cover; a second one in the body would print twice."""
    html = build_report_html(title="Квартальний звіт", sections=[_data_section()], language="uk")
    assert html.count("<h1>") == 1


# --- the wide table --------------------------------------------------------------


def test_a_wide_data_section_asks_for_a_landscape_page() -> None:
    """Step 2 shipped twelve columns wrapping their headings mid-word.

    A portrait A4 gives twelve columns under 15 mm each; the landscape page is
    the answer the design deferred to this step, and something fpdf2 could not
    express at all.
    """
    wide = QueryResult(
        columns=[f"col_{index}" for index in range(12)],
        rows=[tuple(range(12))],
    )
    html = build_report_html(title="Звіт", sections=[_data_section(result=wide)], language="uk")
    assert "@page landscape" in html
    assert "landscape" in html[html.index("<section") : html.index("</section>")]


def test_a_narrow_data_section_stays_portrait() -> None:
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "landscape" not in html[html.index("<section") : html.index("</section>")]


def test_a_text_section_never_goes_landscape_however_long_it_is() -> None:
    """The rule counts columns, and prose has none — an easy off-by-one to write."""
    section = SectionResult(
        title="Політика",
        source="documents",
        sub_question="q",
        kind="text",
        prose="Текст " * 400,
    )
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "landscape" not in html[html.index("<section") : html.index("</section>")]


def test_the_stylesheet_carries_rules_for_the_callout_and_the_page_break() -> None:
    """Added before the block renderers that use them, so the goldens captured
    next already contain them and the inversion can be held to byte-identity."""
    css = stylesheet(resolve_style("kvarta"))
    assert ".callout" in css
    assert ".pagebreak" in css
    assert "break-after: page" in css


# --- blocks drive the document --------------------------------------------------


def test_a_supplied_block_list_controls_the_order_of_the_document() -> None:
    """The whole point of the step: structure comes from the list, not from the
    renderer's fixed idea of what a section contains."""
    section = _data_section()
    reversed_order = build_report_html(
        title="Звіт",
        sections=[section],
        language="uk",
        blocks=[
            Block(kind="heading", section=0),
            Block(kind="viz", section=0, variant="table"),
            Block(kind="stats", section=0),
        ],
    )
    assert reversed_order.index("<table") < reversed_order.index('class="stats"')


def test_an_omitted_block_is_absent_from_the_document() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0), Block(kind="viz", section=0, variant="table")],
    )
    assert 'class="stats"' not in html


def test_an_unknown_kind_is_skipped_and_the_rest_still_renders() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="hologram"), Block(kind="heading", section=0)],
    )
    assert "Дохід за містами" in html
    assert "hologram" not in html


def test_a_block_naming_a_section_that_does_not_exist_is_skipped() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0), Block(kind="stats", section=99)],
    )
    assert 'class="stats"' not in html
    assert "Дохід за містами" in html


def test_a_callout_renders_its_own_text_escaped() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[],
        language="uk",
        blocks=[Block(kind="callout", text="<script>alert(1)</script>Висновок")],
    )
    assert 'class="callout"' in html
    assert "<script>" not in html
    assert "Висновок" in html


def test_a_page_break_renders() -> None:
    html = build_report_html(
        title="Звіт", sections=[], language="uk", blocks=[Block(kind="pagebreak")]
    )
    assert 'class="pagebreak"' in html


def test_blocks_after_a_heading_nest_inside_its_section() -> None:
    """The grouping rule. Numbering, the landscape page and the section rhythm
    are all attached to <section>, so a flat list breaks three CSS rules at once.
    """
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(), _data_section(title="Другий")],
        language="uk",
        style=resolve_style("kvarta"),
        blocks=[
            Block(kind="heading", section=0),
            Block(kind="stats", section=0),
            Block(kind="heading", section=1),
            Block(kind="stats", section=1),
        ],
    )
    assert html.count("<section") == 2
    first = html.index("<section")
    assert html.count("</section>") == 2
    # each section closes before the next opens
    assert html.index("</section>") < html.index("<section", first + 1)
    assert 'class="stats"' in html[html.index("<section") : html.index("</section>")]


def test_a_sectionless_heading_does_not_orphan_the_blocks_after_it() -> None:
    """A heading whose section does not resolve is skipped and must not close the
    group that is still open — otherwise every block after it renders at top
    level, outside any <section>, losing numbering, the landscape page and the
    section margin.
    """
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        style=resolve_style("kvarta"),
        blocks=[
            Block(kind="heading", section=0),
            Block(kind="stats", section=0),
            Block(kind="heading"),  # no section: skipped, must not close the group
            Block(kind="stats", section=0),
        ],
    )
    assert html.count("<section") == 1
    assert html.count("</section>") == 1
    body = html[html.index("<section") : html.index("</section>") + len("</section>")]
    assert body.count('class="stats"') == 2


# --- 5d: hiding a block -------------------------------------------------------


def test_a_hidden_block_is_absent_from_the_document() -> None:
    """Hide has to reach the printed artifact or it is decoration. The block keeps
    its place in the list — the reader can put it back — and contributes nothing."""
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="stats", section=0, hidden=True),
        Block(kind="viz", section=0, variant="table"),
    ]
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk", blocks=blocks)
    assert 'class="stats"' not in html
    assert "<table" in html
    assert "Дохід за містами" in html


def test_hiding_a_heading_closes_its_section_the_way_deleting_it_would() -> None:
    """A hidden block contributes nothing, and that rule is applied before the
    grouping switch rather than inside it. So a hidden heading opens no
    ``<section>`` and what followed it joins whatever came before — exactly what
    deleting the heading does. Hide and delete differ in reversibility, not in
    the document they produce.
    """
    hidden = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[
            Block(kind="heading", section=0, hidden=True),
            Block(kind="viz", section=0, variant="table"),
        ],
    )
    deleted = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="viz", section=0, variant="table")],
    )
    assert hidden == deleted


# --- step 5e: the reader's own words ------------------------------------------


def _text_section(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "title": "Що показують дані",
        "source": "documents",
        "sub_question": "?",
        "kind": "text",
        "prose": "Дохід зріс на 12% [1].",
        "sources": [SectionSourceRef(number=1, title="Звіт 2024", source_id=uuid.uuid4())],
    }
    return SectionResult(**{**base, **overrides})


def test_a_heading_override_replaces_the_generated_section_title() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0, text="Мій заголовок")],
    )
    assert "<h2>Мій заголовок</h2>" in html
    assert "Дохід за містами" not in html


def test_a_prose_override_replaces_the_generated_paragraph() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[Block(kind="prose", section=0, text="Мій абзац.")],
    )
    assert "Мій абзац." in html
    assert "Дохід зріс" not in html


def test_a_cover_override_renames_the_document_and_its_running_header() -> None:
    """The header is not a second field: ``string-set: doctitle`` publishes the
    cover's own h1 to ``@top-left``, so overriding the title moves both. The
    ``<title>`` follows for the same reason — one document, one name."""
    html = build_report_html(
        title="Згенерована назва",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="cover", text="Квартальний звіт")],
    )
    assert "<h1>Квартальний звіт</h1>" in html
    assert "<title>Квартальний звіт</title>" in html
    assert "Згенерована назва" not in html


def test_a_sources_override_replaces_the_citations_line() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[Block(kind="sources", section=0, text="[1] Внутрішній архів")],
    )
    assert "[1] Внутрішній архів" in html
    assert "Звіт 2024" not in html


def test_dropping_the_override_restores_the_generated_words() -> None:
    """The whole reason an edit is an override rather than a mutation: nothing
    had to keep a second copy of what the model wrote."""
    edited = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[Block(kind="prose", section=0, text="Мій абзац.")],
    )
    restored = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[Block(kind="prose", section=0)],
    )
    assert "Мій абзац." in edited
    assert "Дохід зріс на 12% [1]." in restored


def test_an_override_survives_the_document_with_its_citation_markers_intact() -> None:
    """The parent design's citation round-trip, from the rendering side: ``[1]``
    is literal text, and escaping touches only ``&<>"'`` — so the marker reaches
    the page beside the sources line that resolves it."""
    prose = "Виторг зріс [1], а маржа впала [2]."
    html = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[
            Block(kind="prose", section=0, text=prose),
            Block(kind="sources", section=0),
        ],
    )
    assert prose in html
    assert "[1] Звіт 2024" in html


def test_a_hostile_override_cannot_inject_markup() -> None:
    """Reader-supplied now, not only model-supplied — and stored, so it reaches
    every later render of this report rather than one response."""
    hostile = "<script>alert(1)</script>"
    html = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[
            Block(kind="cover", text=hostile),
            Block(kind="heading", section=0, text=hostile),
            Block(kind="prose", section=0, text=hostile),
            Block(kind="sources", section=0, text=hostile),
        ],
    )
    assert "<script>" not in html
    # Five, not four: the cover's override is also the document's name, so it
    # reaches <title> as well as the h1 — four blocks, five places.
    assert html.count("&lt;script&gt;") == 5


def test_a_hidden_block_renders_neither_its_override_nor_its_section() -> None:
    """Hiding still wins: an override is words for a block that draws, and a
    hidden block does not draw."""
    html = build_report_html(
        title="Звіт",
        sections=[_text_section()],
        language="uk",
        blocks=[Block(kind="prose", section=0, text="Мій абзац.", hidden=True)],
    )
    assert "Мій абзац." not in html
    assert "Дохід зріс" not in html


# --- 5f: image and toc blocks -------------------------------------------------


def _png_bytes() -> bytes:
    """A real 4x3 PNG, captured from Pillow rather than fabricated.

    A hand-written byte string would be a stub in the bad sense: WeasyPrint
    silently drops an image it cannot parse, so a fake would make the
    "renders an image" tests pass for the wrong reason.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 3), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_image_block_renders_a_figure_with_its_caption() -> None:
    asset_id = uuid.uuid4()
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="image", asset_id=asset_id, caption="Рис. 1", width=60, align="right")],
        assets={asset_id: ReportAssetRef(mime="image/png", data=_png_bytes())},
    )
    assert "<figure" in html
    assert "img-block" in html
    assert "Рис. 1" in html
    assert "width:60%" in html.replace(" ", "")
    assert "right" in html
    assert "data:image/png;base64," in html


def test_a_dangling_asset_renders_without_the_image_and_does_not_raise() -> None:
    """Assets can be deleted explicitly, so a design may outlive its picture.

    The absence is asserted, not merely the calm: WeasyPrint drops an image it
    cannot parse and renders everything else, producing a valid,
    complete-looking, pictureless document with nothing raised.
    """
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="image", asset_id=uuid.uuid4())],
        assets={},
    )
    assert "<img" not in html
    assert "img-empty" in html


def test_a_toc_lists_the_headings_and_links_to_them() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(title="Перший"), _data_section(title="Другий")],
        language="uk",
        blocks=[
            Block(kind="toc"),
            Block(kind="heading", section=0),
            Block(kind="heading", section=1),
        ],
    )
    assert 'href="#sec-0"' in html
    assert 'href="#sec-1"' in html
    assert 'id="sec-0"' in html
    assert 'id="sec-1"' in html
    assert "target-counter" in html
    assert "Зміст" in html


def test_a_hidden_heading_is_absent_from_the_toc() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(title="Перший"), _data_section(title="Другий")],
        language="uk",
        blocks=[
            Block(kind="toc"),
            Block(kind="heading", section=0, hidden=True),
            Block(kind="heading", section=1),
        ],
    )
    assert 'href="#sec-0"' not in html
    assert 'href="#sec-1"' in html


def test_a_toc_entry_prefers_the_readers_heading_override() -> None:
    """5e made a heading's words the reader's; the toc must not print the
    model's title beside the reader's heading."""
    html = build_report_html(
        title="Звіт",
        sections=[_data_section(title="Модельна назва")],
        language="uk",
        blocks=[Block(kind="toc"), Block(kind="heading", section=0, text="Моя назва")],
    )
    assert "Моя назва" in html
    assert "Модельна назва" not in html


def test_a_document_without_a_toc_carries_no_anchors_and_no_toc_css() -> None:
    """The identity guard for this task: the eleven byte-identity goldens have
    no toc block, so anchors and the ``target-counter`` rule must be emitted
    only where something actually links to them."""
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk", blocks=None)
    assert "id=" not in html.split("<body>")[1]
    assert "target-counter" not in html
    assert "img-block" not in html


# --- 5f: cover variants, logo, approval, section rules ------------------------


def test_the_default_cover_is_byte_identical_without_a_brand() -> None:
    """The identity guard at the cover: every stored report keeps the one it has.

    ``CoverBrand()`` is what a design with no brand block resolves to, so it must
    render exactly what passing nothing renders.
    """
    plain = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    branded = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", brand=CoverBrand()
    )
    assert plain == branded


def test_a_logo_prints_on_the_cover() -> None:
    asset_id = uuid.uuid4()
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        assets={asset_id: ReportAssetRef(mime="image/png", data=_png_bytes())},
        brand=CoverBrand(logo_asset_id=asset_id),
    )
    assert 'class="logo"' in html
    assert "data:image/png;base64," in html


def test_a_logo_whose_asset_is_gone_leaves_the_cover_unmarked() -> None:
    """Deleting an asset must not blank the cover or raise."""
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        assets={},
        brand=CoverBrand(logo_asset_id=uuid.uuid4()),
    )
    assert "<img" not in html
    assert "Звіт" in html


def test_the_minimal_cover_drops_the_meta_and_approval() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        report_date=datetime.date(2026, 8, 8),
        brand=CoverBrand(variant="minimal", approval=True),
    )
    body = html.split("<body")[1]
    assert "cover minimal" in body
    # Scoped to the body on purpose: a branded document emits the cover CSS,
    # which legitimately names `.approval` in a selector.
    assert "approval" not in body
    assert "08.08.2026" not in body


def test_the_band_cover_wraps_the_title_in_a_banner() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        brand=CoverBrand(variant="band"),
    )
    assert "cover band" in html
    assert "banner" in html


def test_approval_prints_two_named_fields_when_turned_on() -> None:
    html = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", brand=CoverBrand(approval=True)
    )
    assert "Підготував" in html
    assert "Затвердив" in html


def test_approval_is_absent_unless_turned_on() -> None:
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "Підготував" not in html
    assert "approval" not in html


def test_approval_labels_follow_the_documents_language() -> None:
    html = build_report_html(
        title="Report", sections=[_data_section()], language="en", brand=CoverBrand(approval=True)
    )
    assert "Prepared by" in html
    assert "Approved by" in html


def test_an_unknown_cover_variant_falls_back_to_the_full_cover() -> None:
    """Read tolerance: a design written by a later version must still render."""
    html = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", brand=CoverBrand(variant="wat")
    )
    assert "cover minimal" not in html
    assert "cover band" not in html
    assert "Звіт" in html


def test_section_rules_add_a_rule_above_each_heading_only_when_asked() -> None:
    without = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    with_rules = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        style=apply_tokens(resolve_style("kvarta"), StyleTokens(section_rules=True)),
    )
    assert "rules-on" in with_rules
    assert "rules-on" not in without


def test_a_result_whose_key_repeats_states_its_row_count_and_claims_nothing_else() -> None:
    """sec-01 printed Σ 1 205,3 млрд for Apple's revenue: five 2017 rows, one
    per period length, added together. The count is still a fact; the total and
    the mean are not — a mean over double-counted rows is as wrong as the sum."""
    result = QueryResult(
        columns=["fiscal_year", "revenue"],
        rows=[
            (2016, Decimal("215639000000")),
            (2017, Decimal("78351000000")),
            (2017, Decimal("229234000000")),
        ],
    )
    html = build_report_html(title="Звіт", sections=[_data_section(result=result)], language="uk")
    assert "Рядків: 3" in html
    # Named, not notated: a bare `"Σ" not in html` would pass for free once the
    # glyphs were replaced by words, turning this guard into a no-op. The
    # section carries no column_labels, so the heading is the raw column name.
    assert "Сума revenue" not in html
    assert "Середнє revenue" not in html


_OVER_AGGREGATED = QueryResult(
    columns=["fiscal_year", "revenue"],
    rows=[
        (2017, Decimal("458469000000")),
        (2018, Decimal("531190000000")),
        (2019, Decimal("520674000000")),
    ],
)
"""sec-01's shape: one row per year, keys genuinely distinct, every figure
two to three times too large because the SQL summed quarterly, year-to-date and
annual facts together. Three rows, not one: ``aggregates_meaningfully`` is False
for a single row, so a one-row fixture puts no Σ at risk and cannot fail."""


def test_an_over_aggregated_section_warns_and_draws_no_chart() -> None:
    """The table is kept so a reader can see the shape; the chart is not,
    because a chart carries no caveat."""
    section = _data_section(
        result=_OVER_AGGREGATED,
        overlap_warning="apple_financials",
        chart_svg=None,
    )
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "перекриваються" in html
    assert "<figure" not in html
    assert "<table" in html


def test_an_over_aggregated_section_states_its_row_count_and_no_total() -> None:
    """The caveat ends "Підсумок не наведено" — and the statistics line printed
    "Σ Дохід: 1205,3 млрд" directly above it. The count is still a fact; the sum
    and the mean of double-counted rows are not, whatever the keys look like."""
    section = _data_section(
        result=_OVER_AGGREGATED,
        column_labels={"fiscal_year": "Рік", "revenue": "Дохід"},
        overlap_warning="apple_financials",
        chart_svg=None,
    )
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "Рядків: 3" in html
    assert "Сума Дохід" not in html
    assert "Середнє Дохід" not in html


def test_a_flagged_section_keeping_stats_without_a_table_claims_no_total() -> None:
    """A stored design may keep ``stats`` and drop the table — a supported edit.
    The caveat rides with the table, so this arrangement has nothing to explain a
    wrong total with; the total must therefore be gone at source, and the caveat
    CSS must not arrive for a caveat that was never emitted."""
    section = _data_section(
        result=_OVER_AGGREGATED,
        column_labels={"fiscal_year": "Рік", "revenue": "Дохід"},
        overlap_warning="apple_financials",
        chart_svg=None,
    )
    html = build_report_html(
        title="Звіт",
        sections=[section],
        language="uk",
        blocks=[Block(kind="heading", section=0), Block(kind="stats", section=0)],
    )
    assert "Рядків: 3" in html
    assert "Сума Дохід" not in html
    assert "Середнє Дохід" not in html
    assert ".caveat" not in html


def test_a_sound_section_is_byte_identical_to_before() -> None:
    """The caveat and its CSS must be conditional: the byte-identity goldens
    capture whole documents, so an unconditional rule restyles ~470 reports."""
    plain = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "перекриваються" not in plain
    assert ".caveat" not in plain


def test_the_caveat_is_localised_in_english_too() -> None:
    section = _data_section(overlap_warning="apple_financials")
    html = build_report_html(title="Report", sections=[section], language="en")
    assert "overlap" in html


def test_a_flagged_query_with_no_rows_gets_no_caveat_and_no_orphaned_css() -> None:
    """A retry-held query can match nothing and still carry ``overlap_warning``
    from the attempt that found the mixed-duration table. With no rows there are
    no wrong figures on the page, so the caveat would be meaningless — and its
    CSS must not be pulled into the document either, or the rule styles nothing."""
    empty = QueryResult(columns=["fiscal_year", "revenue"], rows=[])
    section = _data_section(result=empty, overlap_warning="apple_financials")
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "У базі даних немає даних" in html
    assert "перекриваються" not in html
    assert ".caveat" not in html


def test_the_band_fill_is_emitted_only_for_the_band_variant() -> None:
    """#173's fill is style-dependent, so it lands in ``_conditional_css``.

    The rasterised guards in ``test_report_render`` cannot catch an unconditional
    rule — ``.cover.band .banner`` matches no other cover's markup, so it would
    paint nothing and every pixel assertion would still pass — while the eleven
    byte-identity goldens capture the whole ``<style>`` and would every one of
    them move. This is the assertion that fails when the condition is dropped.
    """
    band = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", brand=CoverBrand(variant="band")
    )
    assert "background: #eef1f5" in band, "the default template's band fill"
    assert "border-bottom: 2pt solid" in band

    # Branded, so `_COVER_CSS` is emitted — and still no fill.
    other = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        brand=CoverBrand(variant="minimal", approval=True),
    )
    assert ".cover.band .banner { background" not in other
    assert "border-bottom: 2pt solid" not in other

    # Unbranded: `_COVER_CSS` is not emitted at all, so neither is the fill.
    plain = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert ".cover.band" not in plain
    assert "border-bottom: 2pt solid" not in plain


def test_the_band_bleed_is_derived_from_the_templates_own_page_margin() -> None:
    """The negative margin has to match the paper, not a constant.

    ``.cover`` pads 34 mm down; the banner pulls that back plus the page's top
    margin to reach the sheet's edge, and the same amount returns as padding so
    the type does not move. Магістраль's margins are 13/12 mm against Кварта's
    18/16, so a hardcoded bleed would leave one of them short of the edge or
    hanging off it — and a rasterised edge check cannot tell those apart from a
    correct one, because both paint *something* at x=0.
    """
    for key, vertical, horizontal in (("kvarta", 18.0, 16.0), ("mahistral", 13.0, 12.0)):
        html = build_report_html(
            title="Звіт",
            sections=[_data_section()],
            language="uk",
            style=TEMPLATES[key],
            brand=CoverBrand(variant="band"),
        )
        assert f"margin: -34mm -{horizontal}mm 8mm -{horizontal}mm" in html, key
        assert f"padding: 12mm {horizontal}mm 8mm {horizontal}mm" in html, key
        assert f"margin-top: -{vertical + 34}mm" in html, key
        assert f"padding-top: {vertical + 12}mm" in html, key


def test_only_a_cover_that_opens_the_sheet_bleeds_past_the_page_margin() -> None:
    """A cover is not pinned to the top, and the pull-up must not outrun it.

    ``validate_blocks`` accepts blocks before a ``cover``, and ``.cover`` sets
    ``break-after`` only — so such a cover renders inline beneath them. The
    sideways bleed is harmless there; another 13–22 mm of pull-up would drag the
    band up over that content, which is why the top bleed is a ``:first-child``
    rule rather than part of the base one.
    """
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        brand=CoverBrand(variant="band"),
    )
    assert ".cover.band:first-child .banner { margin-top:" in html
    # The base rule keeps the pull-up that only cancels `.cover`'s own padding.
    assert ".cover.band .banner { background" in html
    assert "margin: -34mm" in html


# --- #204: a section built from several databases -----------------------------


def test_a_partial_merge_tells_the_reader_which_database_gave_nothing() -> None:
    """The section is honest only if it says so. Two of three databases answering
    and the third silently missing is the outcome #204 ruled out — it answers
    from part of the data without erroring."""
    section = _data_section(notes=["Немає даних із: Microsoft DB."])
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "Microsoft DB" in html


def test_a_merge_note_does_not_suppress_the_chart() -> None:
    """Unlike the overlapping-periods caveat. The chart is still honest about the
    databases that did answer, so hiding it would withhold a correct picture."""
    section = _data_section(notes=["Немає даних із: Microsoft DB."], chart_svg=b"<svg/>")
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "<figure" in html


def test_a_section_with_no_notes_emits_none() -> None:
    html = build_report_html(title="Звіт", sections=[_data_section()], language="uk")
    assert "merge-note" not in html


def test_a_note_is_escaped_like_every_other_reader_facing_string() -> None:
    """A note carries a database *name*, which an admin typed."""
    section = _data_section(notes=["<script>alert(1)</script>"])
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "<script>alert(1)</script>" not in html


def test_a_merged_section_that_found_nothing_still_names_the_databases() -> None:
    """The empty-table path must not swallow the explanation for the emptiness."""
    section = _data_section(
        result=QueryResult(columns=["a"], rows=[]),
        notes=["Немає даних із: Apple DB, Microsoft DB."],
    )
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "Apple DB, Microsoft DB" in html


# --- #205: which database each figure came from -------------------------------


def test_a_merged_chart_names_the_databases_it_drew_from() -> None:
    """The table got its provenance column in #204. The chart — the thing a
    comparison report is actually read for — said nothing at all."""
    section = _data_section(chart_svg=b"<svg/>", databases=["Apple DB", "Microsoft DB"])
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "<figcaption" in html
    assert "Apple DB" in html and "Microsoft DB" in html


def test_a_single_database_chart_gains_no_caption() -> None:
    """Its figures all came from one place. Captioning them would add furniture
    to every report that exists and move all eleven byte-identity goldens."""
    section = _data_section(chart_svg=b"<svg/>", databases=["Apple DB"])
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "<figcaption" not in html


def test_a_chart_with_no_recorded_databases_gains_no_caption() -> None:
    """Every report rendered before #205 — and every documents section."""
    section = _data_section(chart_svg=b"<svg/>")
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "<figcaption" not in html


def test_a_database_name_in_a_caption_is_escaped() -> None:
    """An admin typed it."""
    section = _data_section(
        chart_svg=b"<svg/>", databases=["<script>alert(1)</script>", "Microsoft DB"]
    )
    html = build_report_html(title="Звіт", sections=[section], language="uk")
    assert "<script>alert(1)</script>" not in html


def test_deleting_the_table_leaves_the_chart_still_attributed() -> None:
    """The point of putting provenance INSIDE each block that shows figures: a
    marker a reader can edit away is a citation a reader can edit away."""
    section = _data_section(chart_svg=b"<svg/>", databases=["Apple DB", "Microsoft DB"])
    html = build_report_html(
        title="Звіт",
        sections=[section],
        language="uk",
        blocks=[Block(kind="viz", section=0, variant="chart")],
    )
    assert "<table" not in html
    assert "Apple DB" in html and "Microsoft DB" in html
