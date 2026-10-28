"""Every visual token a report wears, and the five templates that fill them.

Split out so one object drives all three renderers — the HTML document, the
matplotlib figure and the PPTX deck. Before this module the paper colour lived in
``report_html``'s stylesheet, the chart's surface lived in ``report_chart``'s
constants, and the deck had no colour at all; a dark template was impossible
without those three agreeing.

**The palette is not a style choice.** Slots 1-8 are #131's validated categorical
order and a template may not repaint them — the accent reaches
rules, numbering and the first series, nothing else. ``DARK_SERIES`` is the one
exception, and it is not a different palette: it is the same eight hues in the
same slots, snapped into the dark lightness band, because the light steps
measurably fail on a near-black ground (four of eight outside OKLCH L 0.48-0.67,
``#4a3aa7`` at 2.04:1 against ``#14161d``). Both sets were produced by running the
dataviz validator, and both pass every hard check against the papers they are
used on. Re-run before changing either::

    node <dataviz>/scripts/validate_palette.js "<hex,...>" --mode dark --surface "#14161d"

The sub-3:1 contrast WARN the light set carries on light paper is #131's existing
one, relieved as that skill requires: every chart ships with a legend, direct
labels and the table printed beside it. It is not a defect to fix here.

Sizes are in points because the stylesheet and python-pptx both speak points; the
pixel values in the approved prototype were a screen preview, not the artifact.
"""

from dataclasses import dataclass

__all__ = [
    "DARK_SERIES",
    "DEFAULT_TEMPLATE",
    "LIGHT_SERIES",
    "MONO",
    "SANS",
    "SANS_ALT",
    "SERIF",
    "SERIF_ALT",
    "TEMPLATES",
    "ChartTheme",
    "ReportStyle",
    "resolve_style",
]

# Slots 1-8 of the validated categorical order (#131), unchanged.
LIGHT_SERIES = (
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
)
# The same eight hues at OKLCH L 0.66 — inside the dark band, every check PASSes
# against #14161d. Slot order is preserved so a series keeps its identity when a
# reader switches template.
DARK_SERIES = (
    "#4693f3",
    "#e76530",
    "#14ac77",
    "#ca8000",
    "#d56a93",
    "#42ac3c",
    "#8780f4",
    "#f15653",
)
# "Other" is a residual, not a category, and must not wear a categorical hue —
# least of all red, which reads as a status colour.
_OTHER_LIGHT = "#8a8983"
_OTHER_DARK = "#7c8494"

# The three families every template draws from. Each maps to a font file inside
# matplotlib's wheel — see ``report_render._font_css``. No system font is named,
# which is what makes a developer's Mac and the dyno render identically.
SANS = "Report Sans"
SERIF = "Report Serif"
MONO = "Report Mono"
# 5f. Alternate families a pairing token may select. Deliberately NOT a
# redefinition of the three above: those are what the five templates name, and
# repointing them would change four templates' default typeface and move every
# golden. ``report_render._FONT_FILES`` maps both to DejaVu today, same as the
# three names above it — Task 6 repoints just these two entries at real font
# files, once shipped.
SANS_ALT = "Report Sans Alt"
SERIF_ALT = "Report Serif Alt"


@dataclass(frozen=True)
class ChartTheme:
    """The chrome a matplotlib figure draws in. Colours only — never geometry.

    Geometry stays in ``report_chart``: how many panels, how many series, when a
    marker becomes a caterpillar. Those were decided by #131 against the data, not
    against the paper, and a template has no business moving them.
    """

    surface: str
    ink: str
    ink_soft: str
    grid: str
    series: tuple[str, ...]
    other: str


@dataclass(frozen=True)
class ReportStyle:
    """One template: paper, ink, rules, type, spacing, and the chart's chrome."""

    key: str
    name: str
    paper: str
    ink: str
    ink_soft: str
    ink_faint: str
    rule: str
    rule_strong: str
    accent: str
    band: str  # the fill behind a table heading, and the zebra row
    body_family: str
    display_family: str
    numeral_family: str
    h1_pt: float
    h2_pt: float
    body_pt: float
    line_height: float
    page_margin_mm: tuple[float, float]  # vertical, horizontal
    section_gap_mm: float
    numbered_sections: bool
    zebra_rows: bool
    two_column_prose: bool
    chart: ChartTheme
    section_rules: bool = False


_LIGHT_CHART = ChartTheme(
    surface="#fcfcfb",
    ink="#0b0b0b",
    ink_soft="#52514e",
    grid="#dfded9",
    series=LIGHT_SERIES,
    other=_OTHER_LIGHT,
)

TEMPLATES: dict[str, ReportStyle] = {
    # Swiss financial. Numbered sections because a report's sections genuinely are
    # an ordered sequence, and a reader cites them by number.
    "kvarta": ReportStyle(
        key="kvarta",
        name="Кварта",
        paper="#f7f8fa",
        ink="#10151e",
        ink_soft="#3d4759",
        ink_faint="#7b8698",
        rule="#d7dde6",
        rule_strong="#10151e",
        accent="#1a4a75",
        band="#eef1f5",
        body_family=SANS,
        display_family=SANS,
        numeral_family=MONO,
        h1_pt=24.0,
        h2_pt=13.0,
        body_pt=10.0,
        line_height=1.55,
        page_margin_mm=(18.0, 16.0),
        section_gap_mm=8.0,
        numbered_sections=True,
        zebra_rows=True,
        two_column_prose=False,
        chart=ChartTheme(
            surface="#f7f8fa",
            ink="#10151e",
            ink_soft="#3d4759",
            grid="#d7dde6",
            series=LIGHT_SERIES,
            other=_OTHER_LIGHT,
        ),
    ),
    # A deck, not a document: near-black ground, type large enough to read from
    # the back of the room. The only template whose series palette differs.
    "prezydia": ReportStyle(
        key="prezydia",
        name="Президія",
        paper="#14161d",
        ink="#f2f5fa",
        ink_soft="#b3bccb",
        ink_faint="#7b8798",
        rule="#2a3040",
        rule_strong="#4f97db",
        accent="#4f97db",
        band="#1d2130",
        body_family=SANS,
        display_family=SANS,
        numeral_family=MONO,
        h1_pt=30.0,
        h2_pt=17.0,
        body_pt=12.0,
        line_height=1.5,
        page_margin_mm=(18.0, 16.0),
        section_gap_mm=9.0,
        numbered_sections=False,
        zebra_rows=False,
        two_column_prose=False,
        chart=ChartTheme(
            surface="#14161d",
            ink="#f2f5fa",
            ink_soft="#b3bccb",
            grid="#2a3040",
            series=DARK_SERIES,
            other=_OTHER_DARK,
        ),
    ),
    # Dense operational read: narrow margins, mono numerals, data first. The rust
    # accent is a signal colour and is deliberately kept off the data marks.
    "mahistral": ReportStyle(
        key="mahistral",
        name="Магістраль",
        paper="#ffffff",
        ink="#14181d",
        ink_soft="#454f5b",
        ink_faint="#6e7885",
        rule="#e4e8ec",
        rule_strong="#14181d",
        accent="#9e3d16",
        band="#f4f5f6",
        body_family=SANS,
        display_family=SANS,
        numeral_family=MONO,
        h1_pt=20.0,
        h2_pt=12.0,
        body_pt=9.0,
        line_height=1.4,
        page_margin_mm=(13.0, 12.0),
        section_gap_mm=6.0,
        numbered_sections=True,
        zebra_rows=True,
        two_column_prose=False,
        chart=ChartTheme(
            surface="#ffffff",
            ink="#14181d",
            ink_soft="#454f5b",
            grid="#e4e8ec",
            series=LIGHT_SERIES,
            other=_OTHER_LIGHT,
        ),
    ),
    # An analytical read set like a journal: serif, two-column prose, cool grey
    # stock (deliberately not cream — cream is where every generated design lands).
    "chasopys": ReportStyle(
        key="chasopys",
        name="Часопис",
        paper="#f7f7f5",
        ink="#16181a",
        ink_soft="#43474d",
        ink_faint="#767a80",
        rule="#dedfdd",
        rule_strong="#16181a",
        accent="#0a5f56",
        band="#ececeb",
        body_family=SERIF,
        display_family=SERIF,
        numeral_family=MONO,
        h1_pt=26.0,
        h2_pt=15.0,
        body_pt=10.5,
        line_height=1.65,
        page_margin_mm=(20.0, 18.0),
        section_gap_mm=9.0,
        numbered_sections=False,
        zebra_rows=False,
        two_column_prose=True,
        chart=ChartTheme(
            surface="#f7f7f5",
            ink="#16181a",
            ink_soft="#43474d",
            grid="#dedfdd",
            series=LIGHT_SERIES,
            other=_OTHER_LIGHT,
        ),
    ),
    # Whitespace and a strict baseline; hairlines instead of rules, no decoration.
    "minimum": ReportStyle(
        key="minimum",
        name="Мінімум",
        paper="#ffffff",
        ink="#0b0b0c",
        ink_soft="#4a4d52",
        ink_faint="#8b8f95",
        rule="#ececee",
        rule_strong="#0b0b0c",
        accent="#0d4677",
        band="#f6f6f7",
        body_family=SANS,
        display_family=SANS,
        numeral_family=MONO,
        h1_pt=22.0,
        h2_pt=12.5,
        body_pt=10.0,
        line_height=1.7,
        page_margin_mm=(22.0, 20.0),
        section_gap_mm=11.0,
        numbered_sections=False,
        zebra_rows=False,
        two_column_prose=False,
        chart=_LIGHT_CHART,
    ),
}

DEFAULT_TEMPLATE = "kvarta"


def resolve_style(name: str | None) -> ReportStyle:
    """The named template, or the default for an unknown or missing name.

    An unknown name resolves rather than raises on purpose: the key reaches here
    from a stored JSONB column (step 3b), and a report rendered in the wrong
    template is recoverable while one that fails to render is not.
    """
    return TEMPLATES.get(name or "", TEMPLATES[DEFAULT_TEMPLATE])
