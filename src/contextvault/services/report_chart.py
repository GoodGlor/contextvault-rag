"""Render one report section's chart as a PNG (matplotlib/Agg).

Split out of ``report_render`` so the plotting layer can be tested on its own: a
:class:`~matplotlib.figure.Figure` can be interrogated for tick counts, axis
order and series membership, none of which is visible in a PNG byte string.

**The governing rule is that an axis carries its own type.** The renderer used to
stringify every x value, which made every axis categorical, so matplotlib drew
one tick per row — 5,350 daily closing prices became a solid black band where the
dates should have been. Here dates stay dates (a date locator thins them),
numbers stay numbers, and only genuine categories are categorical, where ticks
are thinned explicitly and long label sets flip the bars on their side.

Two measures are never drawn against two y-scales: a second scale invites the
reader to compare two arbitrary calibrations. They become stacked panels sharing
one x-axis instead.
"""

import datetime
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Any, Literal

import matplotlib

matplotlib.use("Agg")

# Deterministic SVG. matplotlib salts the ids it generates for defs it references
# (``<path id="m60d64bd1a7">`` … ``<use xlink:href="#m60d64bd1a7">``) with a fresh
# random value per process, so two renders of one figure differ in bytes while
# being identical in every pixel. A pinned salt makes the document assertable and
# makes a re-render genuinely reproduce its artifact rather than merely resemble it.
# The value is arbitrary; only its constancy matters.
matplotlib.rcParams["svg.hashsalt"] = "contextvault-report"

import matplotlib.dates as mdates  # noqa: E402 — backend must be set before pyplot
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import FuncFormatter, MaxNLocator  # noqa: E402

from contextvault.services.report_execution import QueryResult  # noqa: E402
from contextvault.services.report_format import (  # noqa: E402
    ColumnKind,
    format_tick,
    infer_column_kind,
    label,
)
from contextvault.services.report_llm import ChartSpec  # noqa: E402
from contextvault.services.report_style import ChartTheme, resolve_style  # noqa: E402

XKind = Literal["date", "number", "category"]

# Palette and chrome moved to ``report_style`` in step 3a: the page, the figure
# and the deck have to agree on them, and while they lived here as constants a
# dark template could turn the paper black and leave the picture cream. Slots 1-8
# of the validated categorical order are still what a chart draws with — charts
# here are lines and bars, whose neighbours are adjacent series, so the full eight
# are in play. Text never wears a series colour; the mark beside it carries
# identity.

_MAX_PANELS = 3  # measures stacked over one shared x-axis
_MAX_SERIES = 6  # groups drawn inside one panel
_MAX_X_TICKS = 12
_MARKER_LIMIT = 40  # past this, dots stop being points and become a caterpillar
_MAX_BARS = 30
_MAX_VERTICAL_BARS = 12
_MAX_BAR_LABEL = 18  # characters; longer than this and upright labels collide
_MAX_PIE_SLICES = 8
_MIN_PIE_PCT = 3.0  # smaller wedges get no inline percentage — it would not fit
_FIG_WIDTH = 9.0
_PANEL_HEIGHT = 2.7
_FIRST_PANEL_HEIGHT = 4.0

_ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$")
_WORD = re.compile(r"[^\w]+", re.UNICODE)


@dataclass(frozen=True)
class Series:
    """One drawn line/bar set: a group name (blank when ungrouped) and its values.

    ``values`` is aligned to :attr:`PlotData.x` position by position, with ``nan``
    where the group has no row for that x — so a gap in one company's history
    breaks its line instead of shifting it onto another company's dates.
    """

    label: str
    values: list[float]


@dataclass(frozen=True)
class Panel:
    """One measure and everything drawn for it, on its own y-scale."""

    label: str
    kind: ColumnKind
    series: list[Series]


@dataclass(frozen=True)
class PlotData:
    """Everything the drawing code needs, with no database or LLM types left."""

    x: list[Any]
    x_kind: XKind
    x_label: str
    panels: list[Panel]


def _parse_date(text: str) -> datetime.date | None:
    """``2024-03-31``/``2024-03`` → a date; anything else → ``None``.

    A bare ``2024`` is deliberately not a date: a column of years belongs on a
    numeric axis, where the ticks read as years rather than as January the 1st.
    """
    match = _ISO_DATE.match(text.strip())
    if match is None:
        return None
    try:
        return datetime.date(int(match[1]), int(match[2]), int(match[3] or 1))
    except ValueError:
        return None


def _as_float(value: Any) -> float:
    """A measure as a float; anything unusable becomes ``nan`` (a gap, not a zero).

    The previous renderer substituted ``0.0`` for NULL, which drew a missing
    quarter as a collapse to zero — a factual claim the data never made.
    """
    if value is None or isinstance(value, bool):
        return math.nan
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return math.nan


def _coerce_x(values: list[Any]) -> tuple[XKind, list[Any]]:
    """Give the x-axis its real type, falling back to categories only when it must."""
    if not values or any(value is None for value in values):
        return "category", [str(value) for value in values]
    if all(isinstance(value, (datetime.date, datetime.datetime)) for value in values):
        return "date", list(values)
    if all(isinstance(value, str) for value in values):
        parsed = [_parse_date(value) for value in values]
        if all(date is not None for date in parsed):
            return "date", parsed
        return "category", list(values)
    if all(
        isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) for value in values
    ):
        return "number", [float(value) for value in values]
    return "category", [str(value) for value in values]


def _measure_columns(result: QueryResult, chart: ChartSpec) -> list[str]:
    """The y columns to draw, in order, skipping unknown and duplicated names."""
    names: list[str] = []
    for name in [chart.y_column, *chart.y_columns]:
        if name and name in result.columns and name != chart.x_column and name not in names:
            names.append(name)
    return names[:_MAX_PANELS]


def _series_column(
    result: QueryResult, chart: ChartSpec, *, x_values: list[Any], measures: list[str]
) -> int | None:
    """Index of the column that splits the rows into series, or ``None``.

    Honours the model's own choice first. Failing that, duplicate x values are
    the tell: a query returning both companies' quarters has two rows per date,
    and drawing them as one line produced a zig-zag between the two.
    """
    if (
        chart.series_column
        and chart.series_column in result.columns
        and chart.series_column not in measures
        and chart.series_column != chart.x_column
    ):
        return result.columns.index(chart.series_column)
    if len(set(map(str, x_values))) == len(x_values):
        return None
    for index, name in enumerate(result.columns):
        if name == chart.x_column or name in measures:
            continue
        column = [row[index] for row in result.rows]
        if infer_column_kind(name, column) != "text":
            continue
        if 2 <= len({str(value) for value in column}) <= _MAX_SERIES:
            return index
    return None


def _build_series(
    rows: list[tuple[Any, ...]],
    x_values: list[Any],
    axis: list[Any],
    measure_index: int,
    group_index: int | None,
) -> list[Series]:
    """Align each group's values onto the shared x-axis, leaving gaps as ``nan``."""
    position = {str(value): index for index, value in enumerate(axis)}
    if group_index is None:
        values = [math.nan] * len(axis)
        for row, x_value in zip(rows, x_values, strict=True):
            values[position[str(x_value)]] = _as_float(row[measure_index])
        return [Series(label="", values=values)]

    groups: dict[str, list[float]] = {}
    for row, x_value in zip(rows, x_values, strict=True):
        name = str(row[group_index])
        groups.setdefault(name, [math.nan] * len(axis))[position[str(x_value)]] = _as_float(
            row[measure_index]
        )
    if len(groups) > _MAX_SERIES:
        # Keep the largest by total magnitude, but draw them in the order they
        # first appeared so the colour of a series never depends on its rank.
        ranked = sorted(
            groups,
            key=lambda name: sum(v for v in groups[name] if not math.isnan(v)),
            reverse=True,
        )[:_MAX_SERIES]
        groups = {name: groups[name] for name in groups if name in ranked}
    return [Series(label=name, values=values) for name, values in groups.items()]


def _unique(values: list[Any]) -> list[Any]:
    """Order-preserving unique — already sorted for the continuous axis kinds."""
    seen: set[str] = set()
    axis: list[Any] = []
    for value in values:
        key = str(value)
        if key not in seen:
            seen.add(key)
            axis.append(value)
    return axis


def prepare_plot(
    result: QueryResult, chart: ChartSpec, *, column_labels: dict[str, str] | None = None
) -> PlotData | None:
    """Everything decided before a single mark is drawn, or ``None`` for no chart."""
    if chart.chart_type == "none" or not result.rows:
        return None
    if not chart.x_column or chart.x_column not in result.columns:
        return None
    measures = _measure_columns(result, chart)
    if not measures:
        return None

    labels = column_labels or {}
    x_index = result.columns.index(chart.x_column)
    x_kind, coerced = _coerce_x([row[x_index] for row in result.rows])

    order = list(range(len(result.rows)))
    if x_kind in ("date", "number"):
        # A time axis reads left to right. `ORDER BY … DESC` is what a model
        # writes for "the latest", and it drew 2025 → 2015 across the page.
        order.sort(key=lambda index: coerced[index])
    rows = [result.rows[index] for index in order]
    x_values = [coerced[index] for index in order]
    axis = _unique(x_values)

    group_index = _series_column(result, chart, x_values=x_values, measures=measures)
    panels = [
        Panel(
            label=labels.get(name, name),
            kind=infer_column_kind(name, [row[result.columns.index(name)] for row in result.rows]),
            series=_build_series(rows, x_values, axis, result.columns.index(name), group_index),
        )
        for name in measures
    ]
    return PlotData(
        x=axis,
        x_kind=x_kind,
        x_label=labels.get(chart.x_column, chart.x_column),
        panels=panels,
    )


def _normalise(text: str) -> str:
    return _WORD.sub(" ", (text or "").casefold()).strip()


def keeps_title(chart_title: str, *headings: str | None) -> bool:
    """Whether the chart's own title says anything the surrounding headings did not.

    The PDF prints the report title and the section heading immediately above the
    image, so an echo of either inside the axes is the same sentence twice in two
    type sizes. Both are checked: a one-section report often has the model naming
    the chart after the report.
    """
    chart_text = _normalise(chart_title)
    if not chart_text:
        return False
    for heading in headings:
        heading_text = _normalise(heading or "")
        if heading_text and (chart_text in heading_text or heading_text in chart_text):
            return False
    return True


def _style(ax: Axes, theme: ChartTheme) -> None:
    ax.set_facecolor(theme.surface)
    ax.grid(axis="y", color=theme.grid, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(theme.grid)
    ax.tick_params(colors=theme.ink_soft, labelsize=9, length=0)


def _apply_x_axis(ax: Axes, data: PlotData, *, positions: list[float]) -> None:
    """Ticks a reader can count, whatever the axis is made of."""
    if data.x_kind == "date":
        # matplotlib.dates ships no stubs, unlike matplotlib.ticker — hence the
        # three local ignores rather than an override that would blanket the module.
        locator = mdates.AutoDateLocator(minticks=3, maxticks=_MAX_X_TICKS)  # type: ignore[no-untyped-call]
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))  # type: ignore[no-untyped-call]
        return
    if data.x_kind == "number":
        integral = all(float(value).is_integer() for value in data.x)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=_MAX_X_TICKS, integer=integral))
        return
    # Categorical: matplotlib will happily label all 5,350 of them, so thin the
    # ticks here — this is the step whose absence produced the black band.
    step = max(1, math.ceil(len(data.x) / _MAX_X_TICKS))
    ax.set_xticks(positions[::step])
    ax.set_xticklabels(
        [str(value) for value in data.x[::step]], rotation=30, ha="right", rotation_mode="anchor"
    )


def _plot_positions(data: PlotData) -> list[float]:
    if data.x_kind == "date":
        return [mdates.date2num(value) for value in data.x]  # type: ignore[no-untyped-call]
    if data.x_kind == "number":
        return [float(value) for value in data.x]
    return [float(index) for index in range(len(data.x))]


def _draw_lines(
    ax: Axes, data: PlotData, panel: Panel, positions: list[float], theme: ChartTheme
) -> None:
    marker = "o" if len(data.x) <= _MARKER_LIMIT else None
    for index, series in enumerate(panel.series):
        ax.plot(
            positions,
            series.values,
            color=theme.series[index % len(theme.series)],
            linewidth=2.0,
            marker=marker,
            markersize=4.5,
            label=series.label or None,
            zorder=3,
        )


def _draw_bars(
    ax: Axes, data: PlotData, panel: Panel, positions: list[float], theme: ChartTheme
) -> bool:
    """Draw the bars; return True when they were laid out horizontally.

    Upright bars stop working somewhere around a dozen categories, or sooner when
    the labels are XBRL tags. Past that the chart is turned on its side, where a
    long label has a whole line to itself.
    """
    count = len(panel.series)
    longest = max((len(str(value)) for value in data.x), default=0)
    horizontal = data.x_kind == "category" and (
        len(data.x) > _MAX_VERTICAL_BARS or longest > _MAX_BAR_LABEL
    )
    span = (positions[1] - positions[0]) if len(positions) > 1 else 1.0
    width = abs(span) * 0.8 / max(count, 1)
    for index, series in enumerate(panel.series):
        offset = (index - (count - 1) / 2) * width
        colour = theme.series[index % len(theme.series)]
        if horizontal:
            ax.barh(
                [position + offset for position in positions],
                series.values,
                height=width,
                color=colour,
                label=series.label or None,
                zorder=3,
            )
        else:
            ax.bar(
                [position + offset for position in positions],
                series.values,
                width=width,
                color=colour,
                label=series.label or None,
                zorder=3,
            )
    return horizontal


def _finish_horizontal(
    ax: Axes,
    data: PlotData,
    panel: Panel,
    positions: list[float],
    language: str,
    theme: ChartTheme,
) -> None:
    ax.set_yticks(positions)
    ax.set_yticklabels([str(value) for value in data.x], fontsize=9)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=theme.grid, linewidth=0.8, zorder=0)
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda value, _pos: format_tick(value, panel.kind, language))
    )
    ax.set_xlabel(panel.label, color=theme.ink_soft, fontsize=9)
    ax.set_ylabel("")
    ax.invert_yaxis()  # the first row at the top, where a reader starts


def _cap_bars(data: PlotData) -> tuple[PlotData, int]:
    """Keep the largest ``_MAX_BARS`` categories in their original order.

    Taking the *first* thirty would have dropped the biggest bars off a result
    ordered ascending. Whatever is cut is reported on the axis — a chart that
    silently truncates reads as if it showed everything.
    """
    if len(data.x) <= _MAX_BARS:
        return data, 0

    def magnitude(index: int) -> float:
        values = [series.values[index] for panel in data.panels for series in panel.series]
        return max((abs(value) for value in values if not math.isnan(value)), default=0.0)

    keep = sorted(sorted(range(len(data.x)), key=magnitude, reverse=True)[:_MAX_BARS])
    trimmed = PlotData(
        x=[data.x[index] for index in keep],
        x_kind=data.x_kind,
        x_label=data.x_label,
        panels=[
            Panel(
                panel.label,
                panel.kind,
                [
                    Series(series.label, [series.values[index] for index in keep])
                    for series in panel.series
                ],
            )
            for panel in data.panels
        ],
    )
    return trimmed, len(data.x) - len(keep)


def _readable_on(colour: str, theme: ChartTheme) -> str:
    """White or the template's ink, whichever the wedge can actually be read against.

    The dark fallback is the template's rather than a fixed near-black: on a dark
    template the ink is nearly white, and a pale wedge needs something darker than
    the ink beside it — which is why the light end still falls back to #0b0b0b.
    """
    red, green, blue = (int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5))
    luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
    if luminance < 0.55:
        return "#ffffff"
    return theme.ink if _luminance_of(theme.ink) < 0.55 else "#0b0b0b"


def _luminance_of(colour: str) -> float:
    red, green, blue = (int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _pie_figure(
    data: PlotData, *, language: str, title: str | None, theme: ChartTheme
) -> Figure | None:
    panel = data.panels[0]
    pairs = [
        (str(name), value)
        for name, value in zip(data.x, panel.series[0].values, strict=True)
        if not math.isnan(value) and value > 0
    ]
    if not pairs:
        # A pie of negatives (a balance sheet with a deficit) is not a pie.
        return None
    pairs.sort(key=lambda pair: pair[1], reverse=True)
    colours = list(theme.series[: len(pairs)])
    if len(pairs) > _MAX_PIE_SLICES:
        head = pairs[: _MAX_PIE_SLICES - 1]
        rest = sum(value for _name, value in pairs[_MAX_PIE_SLICES - 1 :])
        pairs = [*head, (label("other", language), rest)]
        colours = [*theme.series[: _MAX_PIE_SLICES - 1], theme.other]

    fig, ax = plt.subplots(figsize=(_FIG_WIDTH, 5.0), facecolor=theme.surface)
    ax.set_facecolor(theme.surface)
    _wedges, _labels, percents = ax.pie(
        [value for _name, value in pairs],
        labels=[name for name, _value in pairs],
        colors=colours,
        startangle=90,
        counterclock=False,
        autopct=lambda pct: f"{pct:.1f}%" if pct >= _MIN_PIE_PCT else "",
        pctdistance=0.72,
        wedgeprops={"edgecolor": theme.surface, "linewidth": 2},
        textprops={"color": theme.ink_soft, "fontsize": 9},
    )
    for text, colour in zip(percents, colours, strict=False):
        text.set_color(_readable_on(colour, theme))
    ax.set_aspect("equal")
    if title:
        ax.set_title(title, loc="left", fontsize=11, color=theme.ink, pad=12)
    return fig


def build_figure(
    result: QueryResult,
    chart: ChartSpec,
    *,
    language: str = "en",
    column_labels: dict[str, str] | None = None,
    section_title: str | None = None,
    report_title: str | None = None,
    theme: ChartTheme | None = None,
) -> Figure | None:
    """The chart as a figure, so tests can read the axes instead of PNG bytes.

    ``theme`` carries the template's paper, ink and series. Omitting it draws the
    default template, which is what every caller predating step 3a does.
    """
    theme = theme or resolve_style(None).chart
    data = prepare_plot(result, chart, column_labels=column_labels)
    if data is None:
        return None
    title = chart.title if keeps_title(chart.title, section_title, report_title) else None
    if chart.chart_type == "pie":
        return _pie_figure(data, language=language, title=title, theme=theme)

    bars = chart.chart_type == "bar"
    dropped = 0
    if bars and data.x_kind == "category":
        data, dropped = _cap_bars(data)
    positions = _plot_positions(data)

    horizontal = (
        bars
        and data.x_kind == "category"
        and (
            len(data.x) > _MAX_VERTICAL_BARS
            or max((len(str(value)) for value in data.x), default=0) > _MAX_BAR_LABEL
        )
    )
    height = (
        max(3.2, 0.3 * len(data.x) + 1.4)
        if horizontal
        else _FIRST_PANEL_HEIGHT + _PANEL_HEIGHT * (len(data.panels) - 1)
    )
    fig, axes = plt.subplots(
        len(data.panels),
        1,
        figsize=(_FIG_WIDTH, height),
        sharex=not horizontal,
        squeeze=False,
        facecolor=theme.surface,
    )
    column = [row[0] for row in axes]
    for index, (ax, panel) in enumerate(zip(column, data.panels, strict=True)):
        _style(ax, theme)
        if bars:
            _draw_bars(ax, data, panel, positions, theme)
        else:
            _draw_lines(ax, data, panel, positions, theme)
        if horizontal:
            _finish_horizontal(ax, data, panel, positions, language, theme)
        else:
            ax.set_ylabel(panel.label, color=theme.ink_soft, fontsize=9)
            ax.ticklabel_format(axis="y", useOffset=False, style="plain")
            ax.yaxis.set_major_formatter(
                FuncFormatter(
                    lambda value, _pos, kind=panel.kind: format_tick(value, kind, language)
                )
            )
            _apply_x_axis(ax, data, positions=positions)
            if index == len(data.panels) - 1:
                ax.set_xlabel(data.x_label, color=theme.ink_soft, fontsize=9)
        if len(panel.series) > 1:
            ax.legend(frameon=False, fontsize=8, labelcolor=theme.ink_soft)
    if dropped:
        note = label("more_categories", language).format(count=dropped)
        bottom = column[-1]
        existing = bottom.get_xlabel()
        bottom.set_xlabel(
            f"{existing} · {note}" if existing else note, color=theme.ink_soft, fontsize=9
        )
    if title:
        column[0].set_title(title, loc="left", fontsize=11, color=theme.ink, pad=10)
    fig.tight_layout()
    return fig


@dataclass(frozen=True)
class ChartImages:
    """The same figure in both containers a report needs.

    The HTML/PDF path takes the SVG, so the chart scales with the page. The PPTX
    path cannot: ``report_render_pptx`` embeds a chart with ``add_picture`` and
    python-pptx has no SVG code path at all. Saving twice out of one draw is what
    keeps the document and the deck from showing different pictures.

    The SVG carries its text as outlines — matplotlib's ``svg.fonttype`` default
    is ``path``, deliberately left alone. Keeping it as text would make the labels
    depend on a font the reader's machine may not have, and the positions were
    computed with DejaVu's metrics.
    """

    png: bytes
    svg: bytes


def render_chart_images(
    result: QueryResult,
    chart: ChartSpec,
    *,
    language: str = "en",
    column_labels: dict[str, str] | None = None,
    section_title: str | None = None,
    report_title: str | None = None,
    theme: ChartTheme | None = None,
) -> ChartImages | None:
    """Both encodings of the requested chart, or ``None`` when no chart applies."""
    theme = theme or resolve_style(None).chart
    figure = build_figure(
        result,
        chart,
        language=language,
        column_labels=column_labels,
        section_title=section_title,
        report_title=report_title,
        theme=theme,
    )
    if figure is None:
        return None
    try:
        png = BytesIO()
        # savefig takes its own facecolor and defaults to white: a themed figure
        # saved without it comes back with a white border round a dark chart.
        figure.savefig(png, format="png", dpi=150, bbox_inches="tight", facecolor=theme.surface)
        svg = BytesIO()
        # ``Date`` is the only non-deterministic entry matplotlib writes into the
        # SVG's RDF metadata, and it carries microseconds. Suppressing it costs
        # nothing: the report's own date is on the cover, and this one is invisible.
        figure.savefig(
            svg,
            format="svg",
            bbox_inches="tight",
            facecolor=theme.surface,
            metadata={"Date": None},
        )
        return ChartImages(png=png.getvalue(), svg=svg.getvalue())
    finally:
        plt.close(figure)


def render_chart(
    result: QueryResult,
    chart: ChartSpec,
    *,
    language: str = "en",
    column_labels: dict[str, str] | None = None,
    section_title: str | None = None,
    report_title: str | None = None,
    theme: ChartTheme | None = None,
) -> bytes | None:
    """PNG bytes for the requested chart, or ``None`` when no chart applies."""
    images = render_chart_images(
        result,
        chart,
        language=language,
        column_labels=column_labels,
        section_title=section_title,
        report_title=report_title,
        theme=theme,
    )
    return None if images is None else images.png
