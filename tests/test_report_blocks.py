"""The block model: what a stored design's block list means.

Writes reject and reads tolerate, inherited verbatim from ``report_design``.
The version is the one departure, and it is validated rather than merely
written — the objection recorded in ``report_design``'s docstring was to
*unvalidated* versions, and it was right.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from contextvault.services.report_blocks import (
    BLOCKS_VERSION,
    MAX_BLOCK_TEXT,
    Block,
    BlockIn,
    allowed_chart_types,
    chart_type_overrides,
    default_blocks,
    default_blocks_from_storage,
    document_title,
    draws_chart,
    load_blocks,
    validate_blocks,
)
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_types import SectionResult, SectionSourceRef


def _design(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "template": "kvarta",
        "blocks_version": BLOCKS_VERSION,
        "blocks": [{"kind": "cover"}, {"kind": "heading", "section": 0}],
    }
    return {**base, **overrides}


def test_a_stored_block_list_loads() -> None:
    blocks = load_blocks(_design())
    assert blocks is not None
    assert [block.kind for block in blocks] == ["cover", "heading"]
    assert blocks[1].section == 0


def test_a_design_with_no_blocks_loads_as_none() -> None:
    """None means 'unedited', which is what makes the caller derive the default."""
    assert load_blocks({"template": "kvarta"}) is None
    assert load_blocks(None) is None


def test_an_unrecognised_version_discards_the_block_list_entirely() -> None:
    """The point of validating it. A payload whose keys may have changed meaning
    renders as if unedited rather than as a document assembled from guesses."""
    assert load_blocks(_design(blocks_version=BLOCKS_VERSION + 1)) is None


def test_a_block_list_with_no_version_is_discarded() -> None:
    raw = _design()
    del raw["blocks_version"]
    assert load_blocks(raw) is None


def test_an_unknown_kind_survives_the_load_and_is_left_for_the_renderer_to_skip() -> None:
    """Reads tolerate: a kind dropped from the code in a later step must not turn
    every report that used it into a 500."""
    blocks = load_blocks(_design(blocks=[{"kind": "cover"}, {"kind": "hologram"}]))
    assert blocks is not None
    assert [block.kind for block in blocks] == ["cover", "hologram"]


def test_a_malformed_block_list_discards_the_whole_list_rather_than_raising() -> None:
    assert load_blocks(_design(blocks="not a list")) is None
    assert load_blocks(_design(blocks=[{"section": "not an int"}])) is None


def test_unknown_keys_on_a_block_are_ignored() -> None:
    blocks = load_blocks(_design(blocks=[{"kind": "cover", "invented": 1}]))
    assert blocks is not None
    assert blocks[0].kind == "cover"


_ROWS = QueryResult(columns=["period", "revenue"], rows=[("2021", Decimal("1"))])


def _data(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "title": "Дані",
        "source": "database",
        "sub_question": "?",
        "kind": "data",
        "result": _ROWS,
    }
    return SectionResult(**{**base, **overrides})


def _shape(blocks: list[Block]) -> list[tuple[str, str | None]]:
    return [(block.kind, block.variant) for block in blocks]


def test_the_default_opens_with_a_cover() -> None:
    assert default_blocks([])[0].kind == "cover"
    assert len(default_blocks([])) == 1


def test_a_charted_data_section_derives_chart_stats_and_table() -> None:
    blocks = default_blocks([_data(chart_svg=b"<svg/>")])
    assert _shape(blocks) == [
        ("cover", None),
        ("heading", None),
        ("viz", "chart"),
        ("stats", None),
        ("viz", "table"),
    ]
    assert all(block.section == 0 for block in blocks[1:])


def test_a_data_section_with_no_chart_derives_no_chart_block() -> None:
    """``chart_svg`` is the signal, and it is only populated once the figure has
    been drawn — which is why the default must be derived inside the builder."""
    assert _shape(default_blocks([_data()])) == [
        ("cover", None),
        ("heading", None),
        ("stats", None),
        ("viz", "table"),
    ]


def test_a_data_section_with_no_rows_derives_only_a_table() -> None:
    """Today an empty result renders one line — the "no rows" note — and neither
    a statistics line nor a chart. The table block carries that note."""
    empty = _data(result=QueryResult(columns=["period"], rows=[]))
    assert _shape(default_blocks([empty])) == [
        ("cover", None),
        ("heading", None),
        ("viz", "table"),
    ]


def test_a_text_section_derives_prose_and_sources_only_when_cited() -> None:
    cited = SectionResult(
        title="Текст",
        source="documents",
        sub_question="?",
        kind="text",
        prose="Абзац[1].",
        sources=[SectionSourceRef(number=1, title="Джерело", source_id=uuid.UUID(int=1))],
    )
    bare = SectionResult(
        title="Текст", source="documents", sub_question="?", kind="text", prose="Абзац."
    )
    assert _shape(default_blocks([cited])) == [
        ("cover", None),
        ("heading", None),
        ("prose", None),
        ("sources", None),
    ]
    assert _shape(default_blocks([bare])) == [
        ("cover", None),
        ("heading", None),
        ("prose", None),
    ]


def test_a_failed_section_derives_an_error_block() -> None:
    failed = SectionResult(
        title="Збій", source="database", sub_question="?", kind="error", error="boom"
    )
    assert _shape(default_blocks([failed])) == [
        ("cover", None),
        ("heading", None),
        ("error", None),
    ]


def test_each_block_binds_to_its_own_section_index() -> None:
    blocks = default_blocks([_data(), _data()])
    headings = [block for block in blocks if block.kind == "heading"]
    assert [block.section for block in headings] == [0, 1]


def test_the_default_never_derives_a_callout_or_a_page_break() -> None:
    """Both exist only for a stored list to use; nothing in a generated report
    produces one."""
    kinds = {block.kind for block in default_blocks([_data(chart_svg=b"<svg/>")])}
    assert "callout" not in kinds
    assert "pagebreak" not in kinds


_TIME = QueryResult(
    columns=["period", "revenue"],
    rows=[
        ("2021-01-01", Decimal("365.8")),
        ("2022-01-01", Decimal("394.3")),
        ("2023-01-01", Decimal("383.3")),
    ],
)
_CATEGORY = QueryResult(
    columns=["segment", "share"],
    rows=[("iPhone", Decimal("52.1")), ("Services", Decimal("22.3")), ("Mac", Decimal("10.2"))],
)


def _charted(result: QueryResult, spec: ChartSpec) -> SectionResult:
    return SectionResult(
        title="Розділ",
        source="database",
        sub_question="?",
        kind="data",
        result=result,
        chart_spec=spec.model_dump(),
    )


def test_a_time_series_is_never_offered_a_pie() -> None:
    """Parent design §3. Nothing enforced this before: prepare_plot and
    build_figure will both happily draw a pie over a date axis, and only the LLM
    prompt discouraged it. A prompt is not a guard."""
    section = _charted(_TIME, ChartSpec(chart_type="line", x_column="period", y_column="revenue"))
    allowed = allowed_chart_types(section)
    assert "pie" not in allowed
    assert {"bar", "line"} <= allowed


def test_a_single_series_composition_is_offered_a_pie() -> None:
    section = _charted(_CATEGORY, ChartSpec(chart_type="bar", x_column="segment", y_column="share"))
    assert "pie" in allowed_chart_types(section)


def test_a_multi_series_result_is_not_offered_a_pie() -> None:
    multi = QueryResult(
        columns=["segment", "company", "share"],
        rows=[
            ("A", "Apple", Decimal("1")),
            ("A", "MSFT", Decimal("2")),
            ("B", "Apple", Decimal("3")),
            ("B", "MSFT", Decimal("4")),
        ],
    )
    section = _charted(
        multi,
        ChartSpec(chart_type="bar", x_column="segment", y_column="share", series_column="company"),
    )
    assert "pie" not in allowed_chart_types(section)


def test_a_section_whose_stored_type_is_none_can_still_be_offered_a_chart() -> None:
    """Measured, and the reason the probe substitutes the candidate rather than
    reading the stored type: prepare_plot returns early on "none", so probing
    with the stored spec would refuse a chart the data supports perfectly well.
    """
    section = _charted(
        _CATEGORY, ChartSpec(chart_type="none", x_column="segment", y_column="share")
    )
    assert {"bar", "line", "pie"} <= allowed_chart_types(section)


def test_a_section_with_no_usable_columns_is_offered_nothing() -> None:
    section = _charted(_CATEGORY, ChartSpec(chart_type="none"))
    assert allowed_chart_types(section) == frozenset()


def test_a_section_with_no_chart_spec_at_all_is_offered_nothing() -> None:
    section = SectionResult(
        title="Розділ", source="database", sub_question="?", kind="data", result=_CATEGORY
    )
    assert allowed_chart_types(section) == frozenset()


def test_a_text_section_is_offered_nothing() -> None:
    section = SectionResult(
        title="Текст", source="documents", sub_question="?", kind="text", prose="Абзац."
    )
    assert allowed_chart_types(section) == frozenset()


def test_a_numeric_year_axis_is_never_offered_a_pie() -> None:
    """The gap #131's rule cares about, reached through a number rather than a date.

    ``_coerce_x`` deliberately classifies a bare ``2024`` as ``x_kind == "number"``
    ("a column of years belongs on a numeric axis"), so gating on ``x_kind ==
    "date"`` alone lets a pie be drawn across 2021/2022/2023 — a time series in
    every sense that matters, and the exact chart a pie must never be. Financial
    reports group by ``fiscal_year`` constantly, so this is the common case, not
    an exotic one.
    """
    years = QueryResult(
        columns=["fiscal_year", "revenue"],
        rows=[(2021, Decimal("365.8")), (2022, Decimal("394.3")), (2023, Decimal("383.3"))],
    )
    section = _charted(
        years, ChartSpec(chart_type="bar", x_column="fiscal_year", y_column="revenue")
    )
    allowed = allowed_chart_types(section)
    assert "pie" not in allowed
    assert {"bar", "line"} <= allowed  # the axis is still perfectly chartable


def test_an_abbreviated_year_axis_is_never_offered_a_pie() -> None:
    """The same time series, named the way a real query named it: ``fy``.

    Measured on report ``0501f8ae``, whose ``x_column`` is ``fy``: the section was
    offered ``['bar','line','pie']``. ``_is_period_axis`` asks
    ``is_period_column``, whose name rule needs a ``_`` or a string boundary
    before ``year``/``yr`` — so the abbreviation slipped through and the axis read
    as an ordinary measure. Production landed on ``period_end``, caught by the
    *date* branch rather than by name, which is the only reason it was not biting
    there.
    """
    years = QueryResult(
        columns=["fy", "revenue"],
        rows=[(2021, Decimal("365.8")), (2022, Decimal("394.3")), (2023, Decimal("383.3"))],
    )
    section = _charted(years, ChartSpec(chart_type="bar", x_column="fy", y_column="revenue"))
    allowed = allowed_chart_types(section)
    assert "pie" not in allowed
    assert {"bar", "line"} <= allowed  # the axis is still perfectly chartable


def test_a_text_period_axis_is_never_offered_a_pie() -> None:
    """The other shape a period wears: what ``to_char`` produces, e.g. ``2024-Q1``."""
    quarters = QueryResult(
        columns=["period", "revenue"],
        rows=[("2024-Q1", Decimal("1")), ("2024-Q2", Decimal("2")), ("2024-Q3", Decimal("3"))],
    )
    section = _charted(quarters, ChartSpec(chart_type="bar", x_column="period", y_column="revenue"))
    assert "pie" not in allowed_chart_types(section)


def test_the_override_map_is_keyed_by_section() -> None:
    blocks = [
        Block(kind="viz", section=0, variant="chart", chart_type="bar"),
        Block(kind="viz", section=2, variant="chart", chart_type="pie"),
    ]
    assert chart_type_overrides(blocks) == {0: "bar", 2: "pie"}


def test_a_viz_block_with_no_override_contributes_nothing() -> None:
    assert chart_type_overrides([Block(kind="viz", section=0, variant="chart")]) == {}


def test_a_table_block_never_contributes_an_override() -> None:
    blocks = [Block(kind="viz", section=0, variant="table", chart_type="bar")]
    assert chart_type_overrides(blocks) == {}


def test_the_first_chart_block_for_a_section_wins() -> None:
    """A section draws one figure, so two chart blocks naming it cannot each have
    their own. Documented rather than rejected: the second block still renders,
    showing the same picture."""
    blocks = [
        Block(kind="viz", section=0, variant="chart", chart_type="bar"),
        Block(kind="viz", section=0, variant="chart", chart_type="line"),
    ]
    assert chart_type_overrides(blocks) == {0: "bar"}


def test_no_blocks_means_no_overrides() -> None:
    assert chart_type_overrides(None) == {}


def test_a_valid_block_list_passes() -> None:
    validate_blocks(
        [Block(kind="cover"), Block(kind="heading", section=0)], [_data()], asset_ids=set()
    )


def test_an_unknown_kind_is_rejected_on_write() -> None:
    """Writes reject where reads tolerate: the caller is here to be told."""
    with pytest.raises(ValueError, match="hologram"):
        validate_blocks([Block(kind="hologram")], [_data()], asset_ids=set())


def test_a_section_index_out_of_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="section"):
        validate_blocks([Block(kind="heading", section=5)], [_data()], asset_ids=set())


def test_a_negative_section_index_is_rejected() -> None:
    with pytest.raises(ValueError, match="section"):
        validate_blocks([Block(kind="heading", section=-1)], [_data()], asset_ids=set())


def test_an_unknown_variant_is_rejected() -> None:
    with pytest.raises(ValueError, match="variant"):
        validate_blocks(
            [Block(kind="viz", section=0, variant="hologram")], [_data()], asset_ids=set()
        )


def test_a_chart_type_the_result_cannot_support_is_rejected() -> None:
    section = _charted(_TIME, ChartSpec(chart_type="line", x_column="period", y_column="revenue"))
    with pytest.raises(ValueError, match="pie"):
        validate_blocks(
            [Block(kind="viz", section=0, variant="chart", chart_type="pie")],
            [section],
            asset_ids=set(),
        )


def test_a_chart_type_the_result_supports_is_accepted() -> None:
    section = _charted(_CATEGORY, ChartSpec(chart_type="bar", x_column="segment", y_column="share"))
    validate_blocks(
        [Block(kind="viz", section=0, variant="chart", chart_type="pie")],
        [section],
        asset_ids=set(),
    )


def test_a_section_bound_kind_with_no_section_is_rejected() -> None:
    """{"kind": "heading"} with no section used to be accepted, stored, and then
    render as "" — a write reported as fine that silently disappears."""
    with pytest.raises(ValueError, match="section"):
        validate_blocks([Block(kind="heading")], [_data()], asset_ids=set())


def test_cover_pagebreak_and_callout_do_not_need_a_section() -> None:
    validate_blocks(
        [Block(kind="cover"), Block(kind="pagebreak"), Block(kind="callout", text="done")],
        [],
        asset_ids=set(),
    )


def test_a_misspelled_key_is_rejected_by_the_write_schema_but_tolerated_on_read() -> None:
    """``BlockIn`` is the request schema: a client typo like ``chartType`` must not
    be silently ignored and stored as no override. ``Block`` — used by
    ``load_blocks`` on the read path — stays tolerant, since a stored payload
    (written before this fix, or by any other means) must never fail to load.
    """
    payload = {"kind": "viz", "variant": "chart", "chartType": "pie"}
    with pytest.raises(ValidationError):
        BlockIn.model_validate(payload)

    block = Block.model_validate(payload)
    assert block.chart_type is None


# --- 5d: hiding a block, and deriving a default from storage -----------------


def test_a_hidden_block_round_trips() -> None:
    """Hide is persisted, which is the whole difference from a client-side toggle:
    a hidden block must stay hidden in the artifact the reader prints."""
    blocks = load_blocks(
        _design(blocks=[{"kind": "cover"}, {"kind": "stats", "section": 0, "hidden": True}])
    )
    assert blocks is not None
    assert blocks[0].hidden is False
    assert blocks[1].hidden is True


def test_a_hidden_chart_block_does_not_impose_its_override() -> None:
    """A hidden block contributes nothing to the document, and a chart type is a
    contribution: leaving it in force would let an invisible block repaint the
    figure a visible one draws."""
    blocks = [
        Block(kind="viz", section=0, variant="chart", chart_type="pie", hidden=True),
        Block(kind="viz", section=0, variant="chart", chart_type="line"),
    ]
    assert chart_type_overrides(blocks) == {0: "line"}


def test_draws_chart_agrees_with_the_renderer() -> None:
    """The predicate the editor's derived default rests on, checked against the
    thing it stands in for rather than assumed.

    ``chart_svg`` is never persisted, so a section loaded from storage cannot
    answer "did this draw a figure?" the way ``default_blocks`` asks it. Drawing
    the chart to find out costs ~200 ms per section (5c measured it on the dyno);
    this predicate costs a ``prepare_plot`` probe. It is exact only because
    ``render_chart_images`` returns ``None`` **iff** ``prepare_plot`` does, and
    that is what this test pins — if a second ``return None`` ever appears in
    ``build_figure``, this fails.
    """
    from contextvault.services.report_chart import render_chart_images

    cases = [
        _charted(_TIME, ChartSpec(chart_type="line", x_column="period", y_column="revenue")),
        _charted(_CATEGORY, ChartSpec(chart_type="bar", x_column="segment", y_column="share")),
        _charted(_CATEGORY, ChartSpec(chart_type="none", x_column="segment", y_column="share")),
        _charted(_CATEGORY, ChartSpec(chart_type="none")),
        _data(),
    ]
    for section in cases:
        expected = (
            section.chart_spec is not None
            and render_chart_images(
                section.result,  # type: ignore[arg-type]
                ChartSpec(**section.chart_spec),
                column_labels=section.column_labels,
            )
            is not None
        )
        assert draws_chart(section) is expected, section.chart_spec


def test_the_storage_default_derives_a_chart_block_the_plain_default_cannot() -> None:
    """``default_blocks`` reads ``chart_svg``, which storage does not carry. Given
    sections straight out of ``load_sections`` it would derive a document with no
    figures at all — so the editor needs the same derivation over a predicate."""
    section = _charted(_TIME, ChartSpec(chart_type="line", x_column="period", y_column="revenue"))
    assert section.chart_svg is None

    assert ("viz", "chart") not in _shape(default_blocks([section]))
    assert ("viz", "chart") in _shape(default_blocks_from_storage([section]))


def test_the_storage_default_omits_a_chart_the_data_cannot_draw() -> None:
    section = _charted(_CATEGORY, ChartSpec(chart_type="none"))
    assert ("viz", "chart") not in _shape(default_blocks_from_storage([section]))


# --- step 5e: text overrides --------------------------------------------------


def test_the_kinds_that_carry_their_own_words_accept_text() -> None:
    """``text`` stops being the callout's private field. Every kind listed here
    renders words the reader can reasonably disagree with — a title the model
    chose, a paragraph it wrote — and the override is how they say so."""
    for kind in ("cover", "heading", "prose", "sources"):
        block = Block(kind=kind, section=None if kind == "cover" else 0, text="Мій текст")
        validate_blocks([block], [_data(kind="text", prose="згенеровано")], asset_ids=set())


@pytest.mark.parametrize("kind", ["stats", "viz", "error", "pagebreak"])
def test_text_on_a_computed_block_is_rejected_on_write(kind: str) -> None:
    """The parent design's non-goal, enforced rather than merely not offered.

    Statistics and table cells are derived from the stored result, so a document
    carrying reader-written ones would disagree with the database. A UI that
    declines to show the control is not a restriction — this endpoint takes a
    block list the client composes, so the refusal has to live at the write gate.
    """
    block = Block(kind=kind, section=None if kind == "pagebreak" else 0, variant="table", text="7")
    with pytest.raises(ValueError, match="text"):
        validate_blocks([block], [_data()], asset_ids=set())


def test_a_computed_block_without_text_still_passes() -> None:
    """The guard rejects the override, not the kind."""
    validate_blocks(
        [Block(kind="stats", section=0), Block(kind="pagebreak")], [_data()], asset_ids=set()
    )


def test_an_empty_override_is_a_real_override_and_is_accepted() -> None:
    """ "" means the reader emptied this block, which is not the same request as
    dropping the key — that one restores the generated words. A guard testing
    truthiness would silently turn the first into the second."""
    validate_blocks([Block(kind="prose", section=0, text="")], [_data()], asset_ids=set())


def test_an_empty_override_on_a_computed_block_is_still_rejected() -> None:
    """The isolating test for ``is not None`` rather than truthiness.

    ``test_an_empty_override_is_a_real_override_and_is_accepted`` above cannot
    catch a truthiness guard: it asserts "" is accepted, which a guard that skips
    "" entirely also does. Replacing the check with ``if block.text:`` therefore
    left every test green while quietly letting ``{"kind": "stats", "text": ""}``
    through — a computed block carrying an override, which is the one thing the
    gate exists to refuse. Found by the mutation pass.
    """
    with pytest.raises(ValueError, match="text"):
        validate_blocks([Block(kind="stats", section=0, text="")], [_data()], asset_ids=set())


def test_an_override_longer_than_the_cap_is_rejected() -> None:
    """Unbounded reader input landing in a JSONB column read on every render."""
    with pytest.raises(ValueError, match="long"):
        validate_blocks(
            [Block(kind="prose", section=0, text="x" * (MAX_BLOCK_TEXT + 1))],
            [_data()],
            asset_ids=set(),
        )


def test_an_override_at_exactly_the_cap_is_accepted() -> None:
    validate_blocks(
        [Block(kind="prose", section=0, text="x" * MAX_BLOCK_TEXT)], [_data()], asset_ids=set()
    )


def test_a_hidden_cover_does_not_name_the_document() -> None:
    """A hidden block contributes nothing, and the document's name is a
    contribution: an invisible cover renaming the running header would be a title
    the reader cannot see, cannot find, and did not ask to keep. Found by the
    mutation pass — dropping the ``not block.hidden`` check broke nothing.
    """
    blocks = [Block(kind="cover", text="Приховано", hidden=True)]
    assert document_title("Згенерована назва", blocks) == "Згенерована назва"


def test_the_first_visible_cover_names_the_document() -> None:
    blocks = [
        Block(kind="cover", text="Приховано", hidden=True),
        Block(kind="cover", text="Видимо"),
    ]
    assert document_title("Згенерована назва", blocks) == "Видимо"


def test_no_cover_override_leaves_the_stored_title_in_place() -> None:
    assert document_title("Звіт", [Block(kind="cover")]) == "Звіт"
    assert document_title("Звіт", None) == "Звіт"


# --- step 5f: toc and image, and the cross-repository asset gate -------------


def test_an_image_block_naming_a_foreign_asset_is_refused() -> None:
    """The access-control property, asserted at the gate rather than in the UI.

    ``PUT …/design`` takes a list the client composes. A palette that lists only
    your own assets is not a restriction — the endpoint is.
    """
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    block = Block(kind="image", asset_id=theirs, width=70, align="center")
    with pytest.raises(ValueError, match="asset"):
        validate_blocks([block], [_data()], asset_ids={mine})


def test_an_image_block_naming_an_owned_asset_is_accepted() -> None:
    mine = uuid.uuid4()
    validate_blocks(
        [Block(kind="image", asset_id=mine, width=70, align="center")],
        [_data()],
        asset_ids={mine},
    )


def test_an_image_block_with_no_asset_is_accepted() -> None:
    """A freshly added block has no picture yet and renders a placeholder."""
    validate_blocks([Block(kind="image")], [_data()], asset_ids=set())


def test_text_is_refused_on_the_new_kinds() -> None:
    """``text`` means "an override of what the model generated". A toc has no
    generated words and an image's words are its ``caption``."""
    for kind in ("toc", "image"):
        with pytest.raises(ValueError, match="text"):
            validate_blocks([Block(kind=kind, text="hi")], [_data()], asset_ids=set())


def test_image_width_outside_the_slider_range_is_refused() -> None:
    for bad in (10, 140):
        with pytest.raises(ValueError, match="width"):
            validate_blocks([Block(kind="image", width=bad)], [_data()], asset_ids=set())


def test_image_align_outside_the_allowed_set_is_refused() -> None:
    with pytest.raises(ValueError, match="align"):
        validate_blocks([Block(kind="image", align="justify")], [_data()], asset_ids=set())


def test_toc_and_image_do_not_need_a_section() -> None:
    validate_blocks([Block(kind="toc"), Block(kind="image")], [], asset_ids=set())


# --- the document's marks (6d) -----------------------------------------------


def test_a_heading_and_a_callout_may_wear_a_mark() -> None:
    blocks = [
        Block(kind="heading", section=0, icon="db"),
        Block(kind="callout", text="!", icon="warn"),
    ]
    validate_blocks(blocks, [_data()], asset_ids=set())

    # The gate passing is not the property — `extra="ignore"` would let this
    # through with the field silently dropped, and the mark would simply never
    # print. What is asserted is that the value survived onto the model.
    assert [block.icon for block in blocks] == ["db", "warn"]


def test_choosing_no_mark_is_a_choice_the_gate_accepts() -> None:
    """``none`` is a reader saying "not this one", which is a different answer
    from never having opened the picker — and both must be storable."""
    validate_blocks([Block(kind="heading", section=0, icon="none")], [_data()], asset_ids=set())


def test_an_unknown_mark_is_refused_while_the_caller_is_present() -> None:
    with pytest.raises(ValueError, match="hologram"):
        validate_blocks(
            [Block(kind="heading", section=0, icon="hologram")],
            [_data()],
            asset_ids=set(),
        )


def test_only_the_two_kinds_the_mockup_marks_may_carry_one() -> None:
    """A mark on a table would be decoration on a computed value, and the series'
    rule is that the document does not editorialise what the database said."""
    with pytest.raises(ValueError, match="icon"):
        validate_blocks(
            [Block(kind="viz", section=0, variant="table", icon="chart")],
            [_data()],
            asset_ids=set(),
        )


def test_a_block_with_no_mark_stores_exactly_what_it_stored_before() -> None:
    """**The assertion protecting ~470 filed reports.**

    Designs are persisted with ``exclude_defaults=True``. A field defaulting to
    anything other than ``None`` would appear in every payload the next time one
    was written — a silent rewrite of stored documents for a feature none of
    them uses, and the goldens would not see it because they render from blocks
    rather than from stored JSON.
    """
    assert Block(kind="heading", section=0).model_dump(exclude_defaults=True) == {
        "kind": "heading",
        "section": 0,
    }
