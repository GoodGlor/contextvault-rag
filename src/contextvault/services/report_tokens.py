"""The reader's styling choices, folded into a ``ReportStyle``.

Tokens, never CSS. The owner rejected "a box of CSS to fill in", so each control
either picks from a closed set or moves a slider inside a fixed range, and the
result is an ordinary ``ReportStyle`` the renderers already know how to use.

**Defaults are the exact identity.** ``apply_tokens(style, StyleTokens())`` must
return ``style`` unchanged for every template — that is what keeps eleven
byte-identity goldens green and proves these controls are opt-in.

Tolerant on read, but only in the way ``StyleTokens`` itself is tolerant: an
unknown *key* is dropped (``extra="ignore"``), and an unrecognised *value of the
right type* — an unknown ``pairing``, ``density`` or ``accent``, or a
non-finite ``scale``/``margin_mm`` — falls back to the template's own answer
rather than raising. A value of the *wrong type* (``density=5``) still raises
``pydantic.ValidationError`` from the model itself; this module does not catch
that. Whoever eventually loads a ``StyleTokens`` from stored JSONB is
responsible for wrapping that construction in ``try/except ValidationError``,
the way ``load_design`` does — nothing does that yet, because nothing
constructs one from storage yet.
"""

import math
from dataclasses import replace
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from contextvault.services.report_style import (
    MONO,
    SANS_ALT,
    SERIF_ALT,
    ChartTheme,
    ReportStyle,
)

SCALE_MIN, SCALE_MAX = 0.85, 1.30
MARGIN_MIN_MM, MARGIN_MAX_MM = 10.0, 30.0

# (body, display). Numerals stay mono in every pairing — a report's figures are
# read in columns, and tabular numerals are what make a column line up.
#
# ⚠️ These are the *ALT* families, and that is load-bearing. ``SANS``/``SERIF``
# must keep resolving to DejaVu exactly as they do today: they are what the five
# templates name, so redefining them would change the default typeface of four
# templates and move every golden. A pairing token selects a *different* family;
# it never redefines an existing one.
_PAIRINGS: dict[str, tuple[str, str]] = {
    "grotesque": (SANS_ALT, SANS_ALT),
    "serif": (SERIF_ALT, SERIF_ALT),
    "mixed": (SANS_ALT, SERIF_ALT),
}

# (line-height multiplier, section-gap multiplier)
_DENSITIES: dict[str, tuple[float, float]] = {
    "compact": (0.92, 0.75),
    "normal": (1.0, 1.0),
    "airy": (1.10, 1.35),
}

ACCENTS = ("#1d6fa3", "#c2544d", "#0e8a7a", "#8a6bbf", "#d2542a")


class StyleTokens(BaseModel):
    """What the reader chose. Every field defaults to "as the template"."""

    model_config = ConfigDict(extra="ignore")

    pairing: str | None = None
    scale: float | None = None
    density: str | None = None
    margin_mm: float | None = None
    section_rules: bool | None = None
    accent: str | None = None


class _Changes(TypedDict, total=False):
    """The subset of ``ReportStyle`` fields a token can touch.

    A plain ``dict[str, Any]`` type-checks against ``dataclasses.replace``'s
    per-field validation too, but ``Any`` also accepts a wrong-shaped value —
    e.g. a float where ``page_margin_mm`` wants a tuple — and that would only
    fail later, inside a renderer. This dict is this module's entire output;
    it is where the typing should be strictest.
    """

    body_family: str
    display_family: str
    numeral_family: str
    h1_pt: float
    h2_pt: float
    body_pt: float
    line_height: float
    section_gap_mm: float
    page_margin_mm: tuple[float, float]
    section_rules: bool
    accent: str
    chart: ChartTheme


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def apply_tokens(style: ReportStyle, tokens: StyleTokens) -> ReportStyle:
    """``style`` with the reader's tokens applied. Never raises."""
    changes: _Changes = {}

    pairing = _PAIRINGS.get(tokens.pairing or "")
    if pairing is not None:
        body, display = pairing
        changes["body_family"] = body
        changes["display_family"] = display
        changes["numeral_family"] = MONO

    # A non-finite scale (e.g. NaN from a stored value) must be ignored, not
    # clamped: ``max(low, min(high, nan))`` returns ``high``, which would
    # silently turn a corrupt value into the *largest* type on the slider.
    if tokens.scale is not None and math.isfinite(tokens.scale):
        factor = _clamp(tokens.scale, SCALE_MIN, SCALE_MAX)
        changes["h1_pt"] = style.h1_pt * factor
        changes["h2_pt"] = style.h2_pt * factor
        changes["body_pt"] = style.body_pt * factor

    density = _DENSITIES.get(tokens.density or "")
    if density is not None:
        lead_multiplier, gap_multiplier = density
        changes["line_height"] = style.line_height * lead_multiplier
        changes["section_gap_mm"] = style.section_gap_mm * gap_multiplier

    if tokens.margin_mm is not None and math.isfinite(tokens.margin_mm):
        vertical, horizontal = style.page_margin_mm
        wanted = _clamp(tokens.margin_mm, MARGIN_MIN_MM, MARGIN_MAX_MM)
        # The vertical margin moves with the horizontal by the ratio the template
        # already chose, so a template keeps its proportion rather than becoming
        # a generic box.
        changes["page_margin_mm"] = (wanted * vertical / horizontal, wanted)

    if tokens.section_rules is not None:
        changes["section_rules"] = tokens.section_rules

    if tokens.accent is not None and tokens.accent in ACCENTS:
        changes["accent"] = tokens.accent
        # Parent design §4: the accent reaches rules, numbering and the *first*
        # series. The rest of the categorical order stays as #131 validated it.
        changes["chart"] = replace(style.chart, series=(tokens.accent, *style.chart.series[1:]))

    return replace(style, **changes) if changes else style
