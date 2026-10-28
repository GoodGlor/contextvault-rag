"""Chart construction — the axis, the ordering and the series, read off the figure.

Asserting on PNG bytes can only prove that *a* picture was drawn; every defect in
the user's report produced a perfectly valid PNG. These tests interrogate the
figure instead: how many ticks the x-axis carries, which direction time runs, how
many axes a two-measure request produced, and what each line is called.
"""

import datetime
import math
from collections.abc import Sequence
from decimal import Decimal
from typing import cast

import matplotlib.dates as mdates
import pytest
from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle

from contextvault.services.report_chart import (
    build_figure,
    keeps_title,
    prepare_plot,
    render_chart,
    render_chart_images,
)
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_style import LIGHT_SERIES, TEMPLATES


def _daily(rows: int = 5350) -> QueryResult:
    start = datetime.date(2004, 8, 19)
    return QueryResult(
        columns=["trade_date", "close_price"],
        rows=[
            (start + datetime.timedelta(days=index), Decimal(str(10 + index % 97)))
            for index in range(rows)
        ],
    )


def _quarters(count: int = 12, *, descending: bool = False) -> QueryResult:
    rows = [
        (datetime.date(2020 + index // 4, [3, 6, 9, 12][index % 4], 28), Decimal(str(100 + index)))
        for index in range(count)
    ]
    if descending:
        rows.reverse()
    return QueryResult(columns=["quarter_end", "total_revenue"], rows=rows)


LINE = ChartSpec(chart_type="line", x_column="trade_date", y_column="close_price", title="t")


def test_a_long_daily_series_gets_a_countable_number_of_ticks() -> None:
    """The defect in the user's report: 5,350 rows produced 5,350 x labels.

    Stringifying the dates made the axis categorical, and matplotlib labels every
    category. The band of overlapping text was solid black. Two independent
    things now prevent it — the axis is a real date axis whose locator thins
    itself, and the categorical fallback thins explicitly — so both are asserted,
    here and in ``test_a_long_categorical_axis_thins_its_own_ticks``.
    """
    figure = build_figure(_daily(), LINE)
    assert figure is not None
    figure.canvas.draw()
    ax = figure.axes[0]
    assert len(ax.get_xticks()) <= 12, len(ax.get_xticks())


def test_a_long_categorical_axis_thins_its_own_ticks() -> None:
    """The fallback path needs the same guarantee: matplotlib labels every category."""
    result = QueryResult(
        columns=["label", "amount"],
        rows=[(f"позиція {index}", Decimal(str(index))) for index in range(400)],
    )
    figure = build_figure(
        result, ChartSpec(chart_type="line", x_column="label", y_column="amount", title="t")
    )
    assert figure is not None
    figure.canvas.draw()
    ax = figure.axes[0]
    assert len(ax.get_xticks()) <= 12, len(ax.get_xticks())


def test_dates_land_on_a_date_axis_and_years_on_a_numeric_one() -> None:
    dated = prepare_plot(_daily(rows=10), LINE)
    assert dated is not None and dated.x_kind == "date"

    years = QueryResult(
        columns=["fiscal_year", "revenue"],
        rows=[(2022, Decimal("1")), (2023, Decimal("2"))],
    )
    numeric = prepare_plot(
        years, ChartSpec(chart_type="line", x_column="fiscal_year", y_column="revenue", title="t")
    )
    assert numeric is not None and numeric.x_kind == "number"

    text = QueryResult(columns=["city", "revenue"], rows=[("Київ", 1), ("Львів", 2)])
    categorical = prepare_plot(
        text, ChartSpec(chart_type="bar", x_column="city", y_column="revenue", title="t")
    )
    assert categorical is not None and categorical.x_kind == "category"


def test_iso_date_text_is_recognised_but_a_bare_year_is_not() -> None:
    """``to_char`` results are strings; a four-digit year is still a number."""
    as_text = QueryResult(
        columns=["period", "revenue"], rows=[("2024-03-31", 1), ("2024-06-30", 2)]
    )
    plot = prepare_plot(
        as_text, ChartSpec(chart_type="line", x_column="period", y_column="revenue", title="t")
    )
    assert plot is not None and plot.x_kind == "date"

    bare = QueryResult(columns=["period", "revenue"], rows=[("2023", 1), ("2024", 2)])
    plot = prepare_plot(
        bare, ChartSpec(chart_type="line", x_column="period", y_column="revenue", title="t")
    )
    assert plot is not None and plot.x_kind == "category"


def test_a_descending_query_still_draws_time_left_to_right() -> None:
    """`ORDER BY … DESC` is what a model writes for "the latest"; it drew 2025 → 2015."""
    chart = ChartSpec(
        chart_type="line", x_column="quarter_end", y_column="total_revenue", title="t"
    )
    plot = prepare_plot(_quarters(descending=True), chart)
    assert plot is not None
    assert plot.x == sorted(plot.x)
    # and the values travelled with their dates rather than staying put
    assert plot.panels[0].series[0].values[0] == 100.0


def test_markers_disappear_once_the_points_would_overlap() -> None:
    dense = build_figure(_daily(rows=200), LINE)
    sparse = build_figure(_daily(rows=12), LINE)
    assert dense is not None and sparse is not None
    assert dense.axes[0].lines[0].get_marker() in (None, "None")
    assert sparse.axes[0].lines[0].get_marker() == "o"


def test_two_measures_become_two_panels_never_two_y_scales() -> None:
    """ "Volume AND price" used to plot whichever one the model put in y_column."""
    result = QueryResult(
        columns=["trade_date", "close_price", "trade_volume"],
        rows=[
            (datetime.date(2024, 1, index + 1), Decimal(str(100 + index)), 5_000_000 + index)
            for index in range(20)
        ],
    )
    chart = ChartSpec(
        chart_type="line",
        x_column="trade_date",
        y_column="close_price",
        y_columns=["trade_volume"],
        title="t",
    )
    figure = build_figure(
        result,
        chart,
        column_labels={"close_price": "Ціна", "trade_volume": "Обсяг"},
    )
    assert figure is not None
    assert len(figure.axes) == 2
    assert [ax.get_ylabel() for ax in figure.axes] == ["Ціна", "Обсяг"]
    # A twinned pair shares a position; stacked panels do not.
    assert figure.axes[0].get_position().y0 > figure.axes[1].get_position().y0


def _two_companies() -> QueryResult:
    rows = []
    for index in range(6):
        quarter = datetime.date(2023 + index // 4, [3, 6, 9, 12][index % 4], 30)
        rows.append((quarter, "Apple", Decimal(str(90 + index))))
        rows.append((quarter, "Microsoft", Decimal(str(50 + index))))
    return QueryResult(columns=["quarter_end", "company", "revenue"], rows=rows)


def test_a_named_series_column_draws_one_line_per_company() -> None:
    chart = ChartSpec(
        chart_type="line",
        x_column="quarter_end",
        y_column="revenue",
        series_column="company",
        title="t",
    )
    plot = prepare_plot(_two_companies(), chart)
    assert plot is not None
    assert [series.label for series in plot.panels[0].series] == ["Apple", "Microsoft"]
    assert len(plot.x) == 6  # six quarters, not twelve rows


def test_duplicate_x_values_find_the_series_column_without_being_told() -> None:
    """One line zig-zagging between two companies is the failure this prevents."""
    chart = ChartSpec(chart_type="line", x_column="quarter_end", y_column="revenue", title="t")
    plot = prepare_plot(_two_companies(), chart)
    assert plot is not None
    assert [series.label for series in plot.panels[0].series] == ["Apple", "Microsoft"]


def test_a_missing_value_breaks_the_line_instead_of_dropping_it_to_zero() -> None:
    result = QueryResult(
        columns=["quarter_end", "revenue"],
        rows=[
            (datetime.date(2024, 3, 31), Decimal("10")),
            (datetime.date(2024, 6, 30), None),
            (datetime.date(2024, 9, 30), Decimal("12")),
        ],
    )
    plot = prepare_plot(
        result, ChartSpec(chart_type="line", x_column="quarter_end", y_column="revenue", title="t")
    )
    assert plot is not None
    assert math.isnan(plot.panels[0].series[0].values[1])


def test_many_categories_turn_the_bars_on_their_side() -> None:
    result = QueryResult(
        columns=["tag", "value"],
        rows=[(f"us-gaap:VeryLongTagName{index}", Decimal(str(index + 1))) for index in range(24)],
    )
    figure = build_figure(
        result, ChartSpec(chart_type="bar", x_column="tag", y_column="value", title="t")
    )
    assert figure is not None
    ax = figure.axes[0]
    # Turned on its side, every label gets its own line: none is dropped, thinned
    # or overlapped. Upright, twenty-four XBRL tags collided into a wedge.
    assert len(ax.get_yticks()) == 24
    assert [text.get_text() for text in ax.get_yticklabels()][0].startswith("us-gaap:")
    bar = cast(Rectangle, ax.patches[0])
    assert bar.get_width() > bar.get_height()  # lying down, not standing up


def test_bars_keep_the_largest_categories_and_say_what_they_cut() -> None:
    """Taking the first thirty of an ascending result dropped the biggest bars."""
    result = QueryResult(
        columns=["tag", "value"],
        rows=[(f"tag{index}", Decimal(str(index))) for index in range(40)],
    )
    figure = build_figure(
        result,
        ChartSpec(chart_type="bar", x_column="tag", y_column="value", title="t"),
        language="uk",
    )
    assert figure is not None
    labels = [text.get_text() for text in figure.axes[0].get_yticklabels()]
    assert "tag39" in labels and "tag0" not in labels
    assert "10" in figure.axes[0].get_xlabel()  # ten categories disclosed, not dropped silently


def test_a_pie_caps_its_slices_and_names_the_remainder() -> None:
    result = QueryResult(
        columns=["category", "amount"],
        rows=[(f"Категорія {index}", Decimal(str(index + 1))) for index in range(18)],
    )
    figure = build_figure(
        result,
        ChartSpec(chart_type="pie", x_column="category", y_column="amount", title="t"),
        language="uk",
    )
    assert figure is not None
    texts = [text.get_text() for text in figure.axes[0].texts]
    assert "Інше" in texts
    assert len([text for text in texts if text.startswith("Категорія")]) == 7


def test_a_pie_of_non_positive_values_is_no_pie_at_all() -> None:
    result = QueryResult(
        columns=["item", "amount"],
        rows=[("Дефіцит", Decimal("-5")), ("Збиток", Decimal("-2"))],
    )
    assert (
        build_figure(
            result, ChartSpec(chart_type="pie", x_column="item", y_column="amount", title="t")
        )
        is None
    )


def test_a_chart_title_that_echoes_the_section_heading_is_dropped() -> None:
    assert not keeps_title("Дохід за кварталами", "Дохід за кварталами")
    assert not keeps_title("Дохід за кварталами", "Дохід за кварталами Alphabet")
    assert keeps_title("Дохід за кварталами", "Структура активів")
    assert not keeps_title("", "Будь-що")


def test_the_section_title_suppresses_the_in_chart_title() -> None:
    figure = build_figure(
        _quarters(),
        ChartSpec(
            chart_type="line",
            x_column="quarter_end",
            y_column="total_revenue",
            title="Дохід за кварталами",
        ),
        section_title="Дохід за кварталами",
    )
    assert figure is not None
    assert figure.axes[0].get_title() == ""


def test_no_chart_for_an_empty_result_an_unknown_column_or_chart_type_none() -> None:
    empty = QueryResult(columns=["trade_date", "close_price"], rows=[])
    assert render_chart(empty, LINE) is None
    assert render_chart(_daily(rows=5), ChartSpec(chart_type="none", title="t")) is None
    assert (
        render_chart(
            _daily(rows=5),
            ChartSpec(chart_type="line", x_column="ghost", y_column="close_price", title="t"),
        )
        is None
    )


def test_render_chart_still_returns_a_png() -> None:
    png = render_chart(_daily(rows=50), LINE, language="uk")
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_one_draw_yields_both_a_png_and_an_svg_of_the_same_chart() -> None:
    """The PDF path takes the SVG; the PPTX path cannot.

    ``report_render_pptx`` embeds a chart with ``add_picture``, and python-pptx
    has no SVG code path at all — so dropping the PNG would silently break every
    deck. Both come out of one draw so the two artifacts cannot show different
    pictures.
    """
    images = render_chart_images(_daily(rows=50), LINE, language="uk")
    assert images is not None
    assert images.png[:8] == b"\x89PNG\r\n\x1a\n"
    assert b"<svg" in images.svg


def test_chart_svg_carries_its_text_as_outlines_not_as_font_dependent_text() -> None:
    """matplotlib's ``svg.fonttype`` default is ``path``, and that is the decision.

    With text kept as text the SVG names a font family, and a machine without it
    substitutes one whose metrics did not lay these labels out — so they collide.
    Step 4 shows this same SVG in a reader's browser, where that is not
    hypothetical. Outlines cannot drift.

    The label does still appear in the file, inside an XML comment matplotlib
    writes for readability. That is not a font dependency and asserting on it
    would only test matplotlib's commenting; ``<text>`` elements are the thing
    that would make the chart depend on the reader's fonts.
    """
    images = render_chart_images(
        _daily(rows=50), LINE, language="uk", column_labels={"close_price": "Ціна закриття"}
    )
    assert images is not None
    assert b"<text" not in images.svg
    assert b'xlink:href="#DejaVuSans-' in images.svg  # the glyphs, drawn as paths


def test_no_chart_means_neither_buffer() -> None:
    assert render_chart_images(_daily(rows=5), ChartSpec(chart_type="none", title="t")) is None


def test_dates_are_positioned_as_dates_not_as_row_numbers() -> None:
    """A gap in the data must open a gap on the axis, not close ranks."""
    result = QueryResult(
        columns=["quarter_end", "revenue"],
        rows=[
            (datetime.date(2020, 3, 31), Decimal("1")),
            (datetime.date(2020, 6, 30), Decimal("2")),
            (datetime.date(2024, 6, 30), Decimal("3")),
        ],
    )
    figure = build_figure(
        result, ChartSpec(chart_type="line", x_column="quarter_end", y_column="revenue", title="t")
    )
    assert figure is not None
    raw = cast("Sequence[float]", figure.axes[0].lines[0].get_xdata(orig=False))
    xdata = [float(value) for value in raw]
    assert xdata[2] - xdata[1] > 10 * (xdata[1] - xdata[0])
    assert xdata[0] == float(mdates.date2num(datetime.date(2020, 3, 31)))  # type: ignore[no-untyped-call]


# --- the chart wears the template -----------------------------------------------


_THEMED = QueryResult(columns=["city", "revenue"], rows=[("Київ", 120), ("Львів", 80)])
_THEMED_SPEC = ChartSpec(chart_type="bar", x_column="city", y_column="revenue", title="Дохід")


def _rgb(hex_colour: str) -> tuple[float, float, float]:
    return tuple(int(hex_colour[index : index + 2], 16) / 255 for index in (1, 3, 5))  # type: ignore[return-value]


def test_the_figure_draws_on_the_templates_paper_not_a_fixed_one() -> None:
    """A light chart pasted onto a near-black page reads as a rendering failure.

    The colours lived in this module as constants until step 3a, which made a
    dark template impossible: the page could go black while the picture on it
    stayed cream.
    """
    style = TEMPLATES["prezydia"]
    figure = build_figure(_THEMED, _THEMED_SPEC, theme=style.chart)
    assert figure is not None
    try:
        assert figure.get_facecolor()[:3] == pytest.approx(_rgb(style.paper), abs=0.01)
    finally:
        plt.close(figure)


def test_the_dark_template_draws_its_marks_in_the_dark_steps() -> None:
    style = TEMPLATES["prezydia"]
    figure = build_figure(_THEMED, _THEMED_SPEC, theme=style.chart)
    assert figure is not None
    try:
        drawn = {
            tuple(patch.get_facecolor()[:3])
            for ax in figure.axes
            for patch in ax.patches
            if isinstance(patch, Rectangle) and patch.get_width()
        }
        expected = _rgb(style.chart.series[0])
        assert any(colour == pytest.approx(expected, abs=0.01) for colour in drawn), drawn
    finally:
        plt.close(figure)


def test_a_light_template_keeps_the_categorical_order_it_always_had() -> None:
    """Spec §4 again, asserted where it could actually be broken: on the marks."""
    figure = build_figure(_THEMED, _THEMED_SPEC, theme=TEMPLATES["kvarta"].chart)
    assert figure is not None
    try:
        drawn = {
            tuple(patch.get_facecolor()[:3])
            for ax in figure.axes
            for patch in ax.patches
            if isinstance(patch, Rectangle) and patch.get_width()
        }
        assert any(colour == pytest.approx(_rgb(LIGHT_SERIES[0]), abs=0.01) for colour in drawn), (
            drawn
        )
    finally:
        plt.close(figure)


def test_the_saved_images_carry_the_themed_surface() -> None:
    """savefig takes its own facecolor; a themed figure saved untinted goes white."""
    images = render_chart_images(_THEMED, _THEMED_SPEC, theme=TEMPLATES["prezydia"].chart)
    assert images is not None
    assert b"14161d" in images.svg.lower()


def test_omitting_the_theme_draws_the_default_template() -> None:
    figure = build_figure(_THEMED, _THEMED_SPEC)
    assert figure is not None
    try:
        assert figure.get_facecolor()[:3] == pytest.approx(
            _rgb(TEMPLATES["kvarta"].paper), abs=0.01
        )
    finally:
        plt.close(figure)


def test_the_same_figure_renders_to_byte_identical_svg_twice() -> None:
    """Goldens, and the parent design's "a re-render produces byte-identical
    artifacts", both rest on this.

    matplotlib defeats it twice over: it writes <dc:date> with microsecond
    precision into every SVG, and it salts element ids (``<path id="m60d64bd1a7">``)
    per process. Neither changes a pixel and both change the bytes.
    """
    result = QueryResult(
        columns=["period", "revenue"],
        rows=[("2021-01-01", Decimal("365.8")), ("2022-01-01", Decimal("394.3"))],
    )
    spec = ChartSpec(chart_type="line", x_column="period", y_column="revenue")
    first = render_chart_images(result, spec, language="uk")
    second = render_chart_images(result, spec, language="uk")
    assert first is not None and second is not None
    assert first.svg == second.svg
    assert b"dc:date" not in first.svg
