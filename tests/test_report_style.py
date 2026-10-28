"""The style model: five complete templates, and the palette rules that bind them.

The colour assertions here are not taste. They encode two measured facts: the
light categorical order fails on a near-black ground, and the dark order is the
same eight hues moved into the dark lightness band — not a different palette.
"""

import pytest

from contextvault.services.report_style import (
    DARK_SERIES,
    DEFAULT_TEMPLATE,
    LIGHT_SERIES,
    TEMPLATES,
    resolve_style,
)


def test_five_templates_ship_under_the_names_the_prototype_uses() -> None:
    assert set(TEMPLATES) == {"kvarta", "prezydia", "mahistral", "chasopys", "minimum"}


def test_the_default_template_exists() -> None:
    assert DEFAULT_TEMPLATE in TEMPLATES


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_fills_every_token(name: str) -> None:
    """A missing token is a silently unstyled element, not an error."""
    style = TEMPLATES[name]
    for field_name in ("paper", "ink", "ink_soft", "ink_faint", "rule", "rule_strong", "accent"):
        value = getattr(style, field_name)
        assert value.startswith("#") and len(value) == 7, (name, field_name, value)
    assert style.body_pt > 0
    assert style.h1_pt > style.h2_pt > style.body_pt
    assert style.chart.series, name


def test_a_light_template_uses_the_validated_categorical_order_unchanged() -> None:
    """Spec §4: a template supplies paper and ink; it may not repaint the series."""
    assert TEMPLATES["kvarta"].chart.series == LIGHT_SERIES
    assert TEMPLATES["chasopys"].chart.series == LIGHT_SERIES
    assert TEMPLATES["minimum"].chart.series == LIGHT_SERIES


def test_the_dark_template_uses_the_dark_steps_of_the_same_order() -> None:
    """Same eight hues, same slots, moved into the dark lightness band.

    The light steps measurably fail on this paper — four of eight fall outside
    OKLCH L 0.48-0.67 and ``#4a3aa7`` sits at 2.04:1 against ``#14161d``.
    """
    dark = TEMPLATES["prezydia"]
    assert dark.chart.series == DARK_SERIES
    assert dark.chart.series != LIGHT_SERIES
    assert len(DARK_SERIES) == len(LIGHT_SERIES)


def test_the_dark_template_draws_on_its_own_paper() -> None:
    """A chart keeping the light surface would sit in a white box on a black page."""
    dark = TEMPLATES["prezydia"]
    assert dark.chart.surface == dark.paper


def test_an_unknown_or_missing_name_resolves_to_the_default() -> None:
    assert resolve_style(None) is TEMPLATES[DEFAULT_TEMPLATE]
    assert resolve_style("no-such-template") is TEMPLATES[DEFAULT_TEMPLATE]
    assert resolve_style("chasopys") is TEMPLATES["chasopys"]


def _contrast(one: str, other: str) -> float:
    """WCAG 2.x contrast ratio between two ``#rrggbb`` colours.

    Written out rather than borrowed from ``report_chart._luminance_of``, which
    skips the sRGB gamma decode: that helper only has to sort colours into light
    and dark, and this has to state a ratio against a published threshold.
    """

    def luminance(colour: str) -> float:
        channels = [int(colour[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(one), luminance(other)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_cover_type_reads_on_the_band_fill(name: str) -> None:
    """#173 put the cover's eyebrow and title onto ``band``; #146 doubted it would read.

    The eyebrow is ``accent`` at 0.8× body size — small text, so 4.5:1 — and the
    title inherits ``ink``. This is the check #146 asked for and #173 finally
    made checkable, and it is a real guard rather than a restatement: a template
    that repaints ``band`` toward its accent, or darkens an accent toward its
    band, silently degrades the cover of every report using that template, and
    nothing else in the suite would notice.

    Measured at the time of writing: the eyebrow ranges 5.17 (Президія, the
    near-black deck #146 named) to 9.00, the title 14.64 to 18.22.
    """
    style = TEMPLATES[name]
    assert _contrast(style.accent, style.band) >= 4.5, f"{name}: the cover eyebrow"
    assert _contrast(style.ink, style.band) >= 4.5, f"{name}: the cover title"


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_the_band_fill_is_a_tint_of_the_paper_not_a_second_colour(name: str) -> None:
    """Why the cover band can carry the page's own text colours unchanged.

    ``band`` is the fill behind a table heading, and it stays close enough to the
    paper that text crossing from one onto the other keeps its contrast — the
    move costs at most 0.72 today. An accent-strength fill would not: with these
    text colours it measures 1.99–2.83:1, which is why #173 did not use one.
    Bounded on both sides, because a band equal to the paper paints nothing.
    """
    style = TEMPLATES[name]
    ratio = _contrast(style.band, style.paper)
    assert 1.0 < ratio < 1.5, f"{name}: band {style.band} against paper {style.paper} = {ratio}"
