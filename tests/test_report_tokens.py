"""Reader-chosen tokens folded into a ``ReportStyle``.

The load-bearing test is the first one: defaults are the *exact* identity. That
is what lets eleven byte-identity goldens stay green while this step ships, and
what proves the controls are opt-in rather than a silent restyle of every report
already stored.
"""

import pytest

from contextvault.services.report_style import SANS_ALT, SERIF_ALT, TEMPLATES, resolve_style
from contextvault.services.report_tokens import (
    ACCENTS,
    MARGIN_MAX_MM,
    MARGIN_MIN_MM,
    StyleTokens,
    apply_tokens,
)


def test_default_tokens_are_the_identity() -> None:
    """No token set → the template's own style, unchanged, for every template."""
    for slug in TEMPLATES:
        style = resolve_style(slug)
        assert apply_tokens(style, StyleTokens()) == style
        # Not just the empty-token short-circuit: the *apply* branch, fed the
        # template's own values back, must reproduce the style exactly too. This
        # pins float exactness and the margin-ratio round-trip, which a change to
        # the arithmetic (not just the "is it set" guard) could otherwise break
        # without either identity test noticing.
        same = StyleTokens(
            scale=1.0,
            density="normal",
            margin_mm=style.page_margin_mm[1],
            section_rules=False,
        )
        assert apply_tokens(style, same) == style


def test_scale_multiplies_every_type_size_and_nothing_else() -> None:
    style = resolve_style("kvarta")
    scaled = apply_tokens(style, StyleTokens(scale=1.2))
    assert scaled.body_pt == pytest.approx(style.body_pt * 1.2)
    assert scaled.h1_pt == pytest.approx(style.h1_pt * 1.2)
    assert scaled.h2_pt == pytest.approx(style.h2_pt * 1.2)
    assert scaled.page_margin_mm == style.page_margin_mm  # scale is type, not page


def test_scale_is_clamped_to_the_slider_range() -> None:
    style = resolve_style("kvarta")
    assert apply_tokens(style, StyleTokens(scale=9.0)).body_pt == pytest.approx(
        style.body_pt * 1.30
    )
    assert apply_tokens(style, StyleTokens(scale=0.1)).body_pt == pytest.approx(
        style.body_pt * 0.85
    )


def test_density_moves_leading_and_section_gap_together() -> None:
    style = resolve_style("kvarta")
    airy = apply_tokens(style, StyleTokens(density="airy"))
    tight = apply_tokens(style, StyleTokens(density="compact"))
    assert airy.line_height > style.line_height > tight.line_height
    assert airy.section_gap_mm > style.section_gap_mm > tight.section_gap_mm


def test_margin_keeps_the_templates_own_proportion() -> None:
    """A template is not turned into a generic box: the vertical margin moves
    with the horizontal one, by the ratio the template already chose."""
    style = resolve_style("kvarta")  # (18.0, 16.0)
    vertical, horizontal = style.page_margin_mm
    moved = apply_tokens(style, StyleTokens(margin_mm=24.0))
    assert moved.page_margin_mm[1] == pytest.approx(24.0)
    assert moved.page_margin_mm[0] == pytest.approx(24.0 * vertical / horizontal)


@pytest.mark.parametrize("slug", ["kvarta", "prezydia"])
def test_accent_recolours_the_accent_and_the_first_series_only(slug: str) -> None:
    """Parent design §4. The categorical palette was validated for colourblind
    separation and contrast; a brand colour may not repaint it."""
    style = resolve_style(slug)
    branded = apply_tokens(style, StyleTokens(accent="#c2544d"))
    assert branded.accent == "#c2544d"
    assert branded.chart.series[0] == "#c2544d"
    assert branded.chart.series[1:] == style.chart.series[1:]


def test_an_unknown_token_value_is_ignored_rather_than_raising() -> None:
    """Read tolerance: a stored design written by a later version must never
    make an existing report unrenderable."""
    style = resolve_style("kvarta")
    assert apply_tokens(style, StyleTokens(density="galactic", pairing="comic")) == style


@pytest.mark.parametrize(
    ("pairing", "body", "display"),
    [
        ("grotesque", SANS_ALT, SANS_ALT),
        ("serif", SERIF_ALT, SERIF_ALT),
        ("mixed", SANS_ALT, SERIF_ALT),
    ],
)
def test_each_pairing_selects_the_alt_families(pairing: str, body: str, display: str) -> None:
    """A pairing selects a *different* family — never repoints ``SANS``/``SERIF``
    themselves, which the five templates already name."""
    style = resolve_style("kvarta")
    paired = apply_tokens(style, StyleTokens(pairing=pairing))
    assert paired.body_family == body
    assert paired.display_family == display


def test_margin_is_clamped_to_the_slider_range() -> None:
    style = resolve_style("kvarta")  # (18.0, 16.0)
    vertical, horizontal = style.page_margin_mm
    too_small = apply_tokens(style, StyleTokens(margin_mm=1.0))
    too_large = apply_tokens(style, StyleTokens(margin_mm=99.0))
    assert too_small.page_margin_mm[1] == pytest.approx(MARGIN_MIN_MM)
    assert too_small.page_margin_mm[0] == pytest.approx(MARGIN_MIN_MM * vertical / horizontal)
    assert too_large.page_margin_mm[1] == pytest.approx(MARGIN_MAX_MM)
    assert too_large.page_margin_mm[0] == pytest.approx(MARGIN_MAX_MM * vertical / horizontal)


def test_section_rules_token_is_applied_directly() -> None:
    style = resolve_style("kvarta")
    assert style.section_rules is False
    assert apply_tokens(style, StyleTokens(section_rules=True)).section_rules is True
    assert apply_tokens(style, StyleTokens(section_rules=False)).section_rules is False


def test_an_unknown_accent_is_ignored_rather_than_raising() -> None:
    """The accent guard uses ``in ACCENTS`` membership, a different code path
    from the ``dict.get`` fallback the pairing/density guards use."""
    style = resolve_style("kvarta")
    assert "#123456" not in ACCENTS
    assert apply_tokens(style, StyleTokens(accent="#123456")) == style
