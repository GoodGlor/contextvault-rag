"""The report as one self-contained HTML document.

Split from ``report_render`` so the document can be asserted as text. fpdf
compressed its content streams, which made a passing test meaningless once
already; a string cannot hide anything. The document carries its own ``<style>``
block for the same reason — one string is the whole report, which is also what
step 4's editor will hand to a browser.

Everything that decides *content* lives elsewhere and is only called from here:
``report_format`` for labels, formatting and which columns may be totalled, and
``report_chart`` for the picture. This module decides where things go.

**Every string that reaches the markup goes through ``_text``.** fpdf drew text;
HTML executes it, and section titles and prose come from an LLM while cell values
come from a customer's database.
"""

import base64
import datetime
import html as html_lib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from contextvault.services.report_blocks import (
    TEXT_KINDS,
    Block,
    default_blocks,
    document_title,
)
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_fonts import font_slug
from contextvault.services.report_format import (
    clean_text,
    format_value,
    infer_column_kind,
    is_measure,
    label,
    measure_summary,
    rows_note,
    short_error,
)
from contextvault.services.report_icons import icon_path
from contextvault.services.report_style import ReportStyle, resolve_style
from contextvault.services.report_types import ReportAssetRef, SectionResult

__all__ = ["CoverBrand", "ReportAssetRef", "build_report_html", "stylesheet"]


@dataclass(frozen=True)
class CoverBrand:
    """What the reader put on the cover: a mark, a layout, and sign-off lines.

    Every field defaults to "as it always was", so a report with no stored brand
    renders the cover it has always rendered. That is what the goldens assert and
    what keeps ~470 stored reports from restyling.

    Approval fields live here rather than as a block kind. ``_cover_html``'s own
    comment anticipated it: a logo and signature fields belong where a reader can
    turn them on, because putting them in unconditionally would ship furniture
    nobody can remove.
    """

    logo_asset_id: uuid.UUID | None = None
    variant: str = "full"
    approval: bool = False


# Emitted only when the document actually contains the block that needs it. The
# eleven byte-identity goldens capture the whole document *including* its
# ``<style>``, so an unconditional rule here would move every one of them and
# silently restyle ~470 stored reports. Same reasoning as the heading anchors:
# an anchor exists only when something links to it.
_TOC_CSS = """
nav.toc ol { list-style: none; padding: 0; margin: 0; }
nav.toc li { display: flex; align-items: baseline; gap: 2mm; }
nav.toc a { text-decoration: none; color: inherit; flex: 1; }
nav.toc a::after { content: target-counter(attr(href url), page); float: right; }
"""

# `target-counter` is WeasyPrint's, and a browser drops the whole declaration as
# invalid — a contents list with no numbers at all, which reads as broken rather
# than as unpaginated. The canvas does not paginate (spec decision 3), so it says
# so with a dash. Derived from `_TOC_CSS` rather than written out, so the two
# cannot drift in anything but the number.
_TOC_CSS_PREVIEW = _TOC_CSS.replace("target-counter(attr(href url), page)", '"—"')

_COVER_CSS = """
.cover .logo { max-height: 16mm; max-width: 70mm; margin: 0 0 6mm 0; display: block; }
.cover.band .banner { padding: 12mm 0 8mm 0; margin: -34mm 0 8mm 0; }
.cover.minimal { padding-top: 60mm; }
.cover .approval {
  display: grid; grid-template-columns: 1fr 1fr; gap: 4mm 10mm; margin: 14mm 0 0 0;
}
.cover .approval div { display: flex; flex-direction: column; gap: 8mm; }
.cover .approval dt { letter-spacing: .1em; text-transform: uppercase; }
.cover .approval dd { margin: 0; border-bottom: 0.3mm solid currentColor; }
"""


def _band_cover_css(style: ReportStyle) -> str:
    """The fill that makes the ``band`` variant a band (#173).

    The rule above gives ``.banner`` padding and margin only. Nothing in ``src``
    ever gave it a background, so for five templates the variant differed from
    ``full`` by the omitted ``.rule`` and a 34 mm pull-up and nothing else — a
    band that painted no band.

    **That rule stays in ``_COVER_CSS`` although this one fully overrides it**
    (same specificity, emitted after). It is not dead code to tidy away: it sits
    in the ``<style>`` of every branded document, so deleting it would move all
    eleven byte-identity goldens — and a CSS comment saying so would move them
    too, which is why the explanation is here instead.

    **The fill is ``style.band``, not ``style.accent``.** That is the whole of
    #146's contrast worry, settled by measurement rather than deferred again.
    The banner holds the eyebrow, which is ``style.accent`` at 0.8× body size and
    therefore small text needing 4.5:1. On an accent fill the existing text
    colours measure **1.99–2.83:1 across the five templates** — a fail
    everywhere, exactly as #146 guessed. ``style.band`` is a tint of the paper
    (1.07–1.13:1 against it), so every text colour keeps the contrast it already
    had: the eyebrow lands at **5.17:1 on Президія** and 6.1–9.0:1 elsewhere,
    and the title at 14.6–18.2:1. The cost of moving text from paper to band is
    at most −0.72, and nothing crosses a threshold.

    Being a tint is also why the fill alone would not read as a band, so the
    accent returns as the band's bottom edge — at ``.cover .rule``'s own 2pt,
    which is the weight this variant drops. No text ever sits on the accent, so
    no new pairing is introduced.

    The bleed is arithmetic, not a guess. ``.cover`` pads 34 mm down and the
    banner already pulls that back; another ``vertical`` mm reaches the paper's
    top edge and ``horizontal`` mm reaches each side, with the same amounts added
    back as padding so the type does not move by a millimetre.

    **The vertical half of that is scoped to a cover that opens the sheet.** A
    cover is not pinned to the top: ``validate_blocks`` accepts one with blocks
    before it, and ``.cover`` sets ``break-after`` only, so such a cover renders
    inline under whatever precedes it. The sideways bleed is safe there, but
    pulling up past the page margin would drag the band over that content — so
    the top bleed lives in a ``:first-child`` rule and every other cover keeps
    the geometry it has today. In the preview canvas the same selector holds:
    ``_preview_body`` drops ``.cv-head`` exactly when the first visible block is
    the cover, which is the case in which the cover *is* the sheet's first child.
    """
    vertical, horizontal = style.page_margin_mm
    return (
        ".cover.band .banner {"
        f" background: {style.band};"
        f" border-bottom: 2pt solid {style.accent};"
        f" margin: -34mm -{horizontal}mm 8mm -{horizontal}mm;"
        f" padding: 12mm {horizontal}mm 8mm {horizontal}mm;"
        " }"
        ".cover.band:first-child .banner {"
        f" margin-top: -{vertical + 34}mm;"
        f" padding-top: {vertical + 12}mm;"
        " }"
    )


_RULES_CSS = """
.rules-on section h2 { border-top: 0.4mm solid currentColor; padding-top: 2.5mm; }
"""

_IMAGE_CSS = """
figure.img-block { margin: 0 0 3mm 0; display: flex; flex-direction: column; gap: 1.5mm; }
figure.img-block.center { align-items: center; }
figure.img-block.right { align-items: flex-end; }
figure.img-block img { height: auto; }
.img-empty { border: 0.3mm dashed currentColor; padding: 6mm; text-align: center; }
"""

_CAVEAT_CSS = """
p.caveat {
  border-left: 0.6mm solid currentColor; padding: 1.5mm 0 1.5mm 2.5mm;
  margin: 0 0 2.5mm 0; font-style: italic;
}
"""

# 6d. Sized in `em` so a mark follows the heading it sits beside rather than a
# number chosen for one template — Президія's h2 is half again Мінімум's.
# `vertical-align` rather than a flex row: a heading is a line of text, and a
# flex container would break `column-span: all` in a two-column section.
_ICON_CSS = """
.cv-icon {
  width: 1em; height: 1em; vertical-align: -0.12em; margin-right: 1.6mm;
}
.callout .cv-icon { margin-right: 0; margin-bottom: 1mm; display: block; }
"""

_MAX_TABLE_ROWS = 24  # head + tail; the chart, not the table, carries the shape
_MAX_STAT_COLUMNS = 4  # nineteen Σ/x̄ lines pushed the content off the page
# Below this a portrait A4 still gives each column a little over 20 mm at the
# table's size. Above it, headings start wrapping mid-word — which is what step 2
# shipped for a twelve-column result, at under 15 mm a column. A landscape sheet
# buys 82 mm of width, and fpdf2 could not turn a page at all.
_LANDSCAPE_MIN_COLUMNS = 8


def stylesheet(style: ReportStyle) -> str:
    """The whole visual system as one CSS string, derived from the template.

    This was a module constant until step 3a. It carried exactly one template's
    worth of decisions — light paper, a sans face, 10 pt — and every one of them is
    now a token, because a dark deck and a serif journal cannot both be a constant.

    The paper colour is declared on ``@page`` rather than on ``html``. Measured
    against WeasyPrint 69: an ``html`` or ``body`` background paints only the
    content box and leaves a white frame in every margin, which on a near-black
    template reads as a broken renderer while passing every assertion about text.
    """
    vertical, horizontal = style.page_margin_mm
    note_pt = f"{style.body_pt * 0.8:.1f}"
    zebra = f"tbody tr:nth-child(even) {{ background: {style.band}; }}" if style.zebra_rows else ""
    prose_columns = (
        # The heading spans the measure rather than sitting in the left column,
        # which is how a journal sets one. `column-span` is checked, not assumed:
        # WeasyPrint implements it, unlike `text-wrap: balance` above.
        "section.text-section { columns: 2; column-gap: 7mm; }"
        "section.text-section h2 { column-span: all; }"
        if style.two_column_prose
        else ""
    )
    numbering = (
        "body { counter-reset: section-number; }"
        "section.numbered h2::before {"
        " counter-increment: section-number; content: counter(section-number) '. ';"
        f" color: {style.accent}; }}"
        if style.numbered_sections
        else ""
    )
    return f"""
@page {{
    size: A4;
    margin: {vertical}mm {horizontal}mm;
    background: {style.paper};
}}

html {{
    font-family: "{style.body_family}";
    font-size: {style.body_pt}pt;
    line-height: {style.line_height};
    color: {style.ink};
}}
body {{ margin: 0; }}

/* No `text-wrap: balance`: WeasyPrint does not implement it and logs an
   "unknown property" warning on every single render.
   `string-set` publishes the title to the running header — set on the element
   rather than passed in twice, so the header cannot disagree with the cover. */
h1 {{
    font-family: "{style.display_family}"; font-size: {style.h1_pt}pt;
    margin: 0 0 6mm 0; string-set: doctitle content();
}}
h2 {{
    font-family: "{style.display_family}"; font-size: {style.h2_pt}pt;
    margin: 0 0 3mm 0; break-after: avoid;
}}
section {{ margin-bottom: {style.section_gap_mm}mm; }}

/* The cover is a page, not a heading: it ends with a break, so section one
   always opens the second sheet however short the title is. */
.cover {{ break-after: page; padding-top: 34mm; }}
.cover .eyebrow {{
    font-size: {note_pt}pt; letter-spacing: .12em; text-transform: uppercase;
    color: {style.accent}; margin: 0;
}}
.cover h1 {{ font-size: {style.h1_pt * 1.35:.1f}pt; margin: 3mm 0 0 0; }}
.cover .rule {{ height: 2pt; width: 34%; background: {style.accent}; margin: 6mm 0; }}
.cover .meta {{ color: {style.ink_soft}; margin: 0; }}

/* A wide table gets the sheet turned rather than its headings broken mid-word.
   The named page does nothing on its own — `section.landscape` is what claims
   it, and a section only wears that class once it has enough columns to need it.
   The break returns the next section to portrait: without it, whatever follows
   is set on the turned sheet, and a paragraph at landscape measure is a 27 cm
   line. Checked: it adds no trailing blank page when the wide section is last. */
@page landscape {{ size: A4 landscape; }}
section.landscape {{ page: landscape; break-after: page; }}
p {{ margin: 0 0 3mm 0; orphans: 2; widows: 2; }}
.note {{ font-size: {note_pt}pt; color: {style.ink_faint}; }}
.sources {{ font-size: {style.body_pt * 0.9:.1f}pt; color: {style.ink_soft}; }}
.stats {{ margin: 0 0 3mm 0; font-family: "{style.numeral_family}"; }}
.stats span {{ margin-right: 6mm; white-space: nowrap; }}

figure {{ margin: 0 0 4mm 0; break-inside: avoid; }}
figure img {{ width: 100%; height: auto; }}

table {{
    width: 100%; border-collapse: collapse; font-size: {note_pt}pt;
    /* The default `auto` layout does not shrink a wide table: it lets the last
       columns run off the page edge and cuts them there, with no warning. */
    table-layout: fixed;
}}
thead {{ display: table-header-group; }}   /* the header repeats on every page */
/* A row split across a page boundary loses half its cells, and the continuation
   page opens with blanks that read as missing data. */
tr {{ break-inside: avoid; }}
th, td {{
    padding: 1.4mm 1.6mm; border: .4pt solid {style.rule};
    text-align: left; overflow-wrap: anywhere;
}}
th {{ background: {style.band}; font-weight: 700; color: {style.ink}; }}
td.num {{
    text-align: right; font-family: "{style.numeral_family}";
    font-variant-numeric: tabular-nums;
}}
/* A pulled-out conclusion. `break-inside: avoid` because a callout split across
   a page boundary reads as two unrelated remarks. */
.callout {{
    background: {style.band}; border-left: 3pt solid {style.accent};
    padding: 3mm 4mm; margin: 0 0 3mm 0; break-inside: avoid;
}}
/* An explicit break the reader asked for. Nothing derives one. */
.pagebreak {{ break-after: page; }}
{zebra}
{prose_columns}
{numbering}
"""


def _preview_font_css(style: ReportStyle) -> str:
    """The faces the canvas needs, over HTTP.

    The printed document carries none of this: ``report_render`` hands WeasyPrint
    a *separate* stylesheet whose sources are ``file://`` paths, which fontconfig
    can read and a browser may not. Nothing is being rewritten here — these rules
    are added, which is why they cannot move a golden however the flag is set.

    Both weights per family, for the reason ``_font_css`` records: a missing 700
    is synthesised from the regular outlines, so the page shows a fake bold while
    every assertion about text passes.
    """
    return "".join(
        f'@font-face {{ font-family: "{family}"; font-weight: {weight};'
        f' src: url("/api/report-fonts/{font_slug(family, weight)}"); }}'
        for family in sorted({style.body_family, style.display_family, style.numeral_family})
        for weight in (400, 700)
    )


def _preview_css(style: ReportStyle) -> str:
    """The page box a browser cannot get from ``@page``, plus the editor's marks.

    Roughly forty lines, and the only part of the canvas that can drift from the
    printed page — spec decision 1 accepted exactly this much and named it. Every
    value is read off the same ``ReportStyle`` the printed document uses, so what
    can drift is the emulation and never the tokens.

    Three things in ``stylesheet()``'s output are hostile to a browser and are
    answered here: ``@page``'s size, margins and paper become a box; its margin
    boxes become a static header and footer; and ``html { … }`` is restated on
    ``.cv-sheet`` because the canvas mounts into a **shadow root**, where there is
    no ``html`` element for that rule to match. The restatement uses the same
    tokens rather than a second set of them.

    ``outline`` and never ``border`` for the marks: a border is layout, and would
    move the very thing the canvas exists to show you.
    """
    vertical, horizontal = style.page_margin_mm
    small = f"{style.body_pt * 0.8:.1f}"
    return f"""
.cv-sheet {{
    width: 210mm; box-sizing: border-box; margin: 0 auto;
    /* The canvas's scale, set as `--cv-zoom` on the host and inherited across the
       shadow boundary (#168). `zoom` and not `transform`: a transform paints a
       scaled picture of an unchanged layout box, so at 125 % the half that
       overflows cannot be scrolled to, and rects would still have to be
       compensated. `zoom` resizes the box, so `overflow-x: auto` sees it and
       `getBoundingClientRect()` comes back already scaled — which is what lets the
       floating toolbar keep its arithmetic. The fallback keeps every other caller
       life-size. */
    zoom: var(--cv-zoom, 1);
    padding: {vertical}mm {horizontal}mm;
    background: {style.paper};
    font-family: "{style.body_family}";
    font-size: {style.body_pt}pt;
    line-height: {style.line_height};
    color: {style.ink};
}}
.cv-head, .cv-foot {{
    font-family: "{style.body_family}"; font-size: {small}pt;
    color: {style.ink_faint};
    display: flex; justify-content: space-between;
}}
.cv-head {{ margin: 0 0 8mm 0; }}
.cv-foot {{ margin: 8mm 0 0 0; }}
[data-block-index] {{ cursor: pointer; }}
[data-block-index]:hover {{ outline: 1px dashed {style.accent}; outline-offset: 2px; }}
[data-block-index].cv-selected {{ outline: 2px solid {style.accent}; outline-offset: 3px; }}
[contenteditable="true"] {{ cursor: text; }}
/* The mark appears when a reader reaches for it, not at rest (#167). Shown
   permanently, the document never looks like the document — and the cover's box
   is the largest object on the sheet. */
[contenteditable="true"]:hover {{
    outline: 1px dashed {style.ink_faint}; outline-offset: 2px;
}}
[contenteditable="true"]:focus {{
    outline: 2px solid {style.accent}; outline-offset: 2px;
    /* A short rule in the margin, the way a proof is marked up. `box-shadow`
       and not a border, for the reason every mark here uses outline: a border
       is layout, and would move the line being edited. */
    box-shadow: -10px 0 0 -7px {style.accent};
}}
"""


def _page_css(style: ReportStyle, language: str) -> str:
    """The page footer — the one rule carrying a word, so it is built per report.

    An English word inside an otherwise Ukrainian report is a defect this suite
    already guards against elsewhere; the footer is not exempt.
    """
    small = f"{style.body_pt * 0.8:.1f}"
    return (
        "@page { @bottom-right {"
        f' content: "{label("page", language)} " counter(page) " / " counter(pages);'
        f' font-family: "{style.body_family}"; font-size: {small}pt;'
        f" color: {style.ink_faint}; }}"
        # The running header names the document on every interior sheet — the mark
        # that separates a filed record from a printout.
        " @top-left { content: string(doctitle);"
        f' font-family: "{style.body_family}"; font-size: {small}pt;'
        f" color: {style.ink_faint}; }} }}"
        # A cover carrying its own title in the header would print it twice, once
        # large and once small, three centimetres apart.
        " @page :first { @top-left { content: none; } }"
    )


def _text(value: Any) -> str:
    """Every string that reaches the markup, cleaned and escaped.

    ``clean_text`` strips the control characters a model reply carries in;
    ``escape`` is new with HTML, and having one funnel is what makes "did we
    escape this one?" answerable by reading rather than by auditing call sites.
    """
    return html_lib.escape(clean_text(str(value)))


def _numeric_stats(
    result: QueryResult,
    *,
    language: str,
    column_labels: dict[str, str],
    overlapping: bool = False,
) -> list[tuple[str, str]]:
    """The document's statistics block: ``measure_summary`` at this page's width.

    The decision — which columns are measures, when Σ is a lie, when x̄ is —
    belongs to ``report_format.measure_summary`` and is shared with the deck.
    What is local here is ``_MAX_STAT_COLUMNS``: an A4 page fits four, and the
    reason is that nineteen Σ/x̄ lines once pushed the content off it.

    ``overlapping`` is passed through rather than derived; see ``measure_summary``
    for why it cannot be computed from the rows.
    """
    return measure_summary(
        result.columns,
        result.rows,
        language=language,
        column_labels=column_labels,
        max_columns=_MAX_STAT_COLUMNS,
        overlapping=overlapping,
    )


def _row_window(rows: list[Any], limit: int) -> tuple[list[Any], int, list[Any]]:
    """Head, count-omitted, tail — so a long result shows both of its ends."""
    if len(rows) <= limit:
        return list(rows), 0, []
    half = limit // 2
    return list(rows[:half]), len(rows) - 2 * half, list(rows[-half:])


def _stats_html(
    result: QueryResult,
    *,
    language: str,
    labels: dict[str, str],
    at: str = "",
    overlapping: bool = False,
) -> str:
    """The summary line, with a break opportunity between its parts.

    The spans are joined with a space, not concatenated. Each is
    ``white-space: nowrap`` so a figure never splits from its label, and with no
    whitespace between them the line had no legal break point at all: on a
    template with larger type the last statistic ran straight off the right edge
    of the page. Extracted text shows it in full either way.
    """
    pairs = _numeric_stats(result, language=language, column_labels=labels, overlapping=overlapping)
    spans = " ".join(f"<span>{_text(name)}: {_text(value)}</span>" for name, value in pairs)
    return f'<div class="stats"{at}>{spans}</div>'


def _table_html(result: QueryResult, *, language: str, labels: dict[str, str], at: str = "") -> str:
    """The result as one table — no column measuring and no wrapping into blocks.

    fpdf2 needed both because it does not clip: a cell handed more text than it
    could hold painted over its neighbour. CSS clips at the cell edge and
    ``overflow-wrap: anywhere`` wraps inside it, so the arithmetic goes away — but
    only under ``table-layout: fixed``, because the default lets a wide table run
    off the page and cuts it there instead.
    """
    kinds = [
        infer_column_kind(name, [row[index] for row in result.rows])
        for index, name in enumerate(result.columns)
    ]
    heading = "".join(f"<th>{_text(labels.get(name, name))}</th>" for name in result.columns)

    def body(rows: list[Any]) -> str:
        out = []
        for row in rows:
            cells = []
            for index in range(len(result.columns)):
                value = _text(format_value(row[index], kinds[index], language))
                css = ' class="num"' if is_measure(kinds[index]) else ""
                cells.append(f"<td{css}>{value}</td>")
            out.append(f"<tr>{''.join(cells)}</tr>")
        return "".join(out)

    head, omitted, tail = _row_window(result.rows, _MAX_TABLE_ROWS)
    middle = ""
    if omitted:
        note = _text(rows_note("middle_rows", omitted, language))
        middle = f'<tr><td colspan="{len(result.columns)}" class="note">{note}</td></tr>'
    return (
        f"<table{at}><thead><tr>{heading}</tr></thead>"
        f"<tbody>{body(head)}{middle}{body(tail)}</tbody></table>"
    )


def _chart_html(section: SectionResult, *, language: str, at: str = "") -> str:
    """The chart as a data URI, or nothing at all.

    A data URI rather than a temporary file so the document stays one string —
    which is what makes it assertable, and what step 4 hands to a browser.

    A section built from **several** databases captions the figure with their
    names (#205). The chart is what a comparison report is read for, and
    ``series_column`` splits *entities*, not sources — so without this the
    picture carried no provenance at all while the table beside it did. The
    caption lives inside the ``<figure>`` deliberately: provenance a reader can
    delete separately is a citation a reader can delete separately, and each
    block that shows the figures states it independently.
    """
    if section.chart_svg is None:
        return ""
    encoded = base64.b64encode(section.chart_svg).decode("ascii")
    caption = ""
    if len(section.databases) > 1:
        names = ", ".join(_text(name) for name in section.databases)
        caption = (
            f"<figcaption>{_text(label('drawn_from', language)).format(names=names)}</figcaption>"
        )
    return (
        f'<figure{at}><img src="data:image/svg+xml;base64,{encoded}"'
        f' alt="{_text(section.title)}">{caption}</figure>'
    )


def _icon_html(name: str | None, *, editable: bool, stroke: str = "currentColor") -> str:
    """A block's mark, or "" for no mark and for a name this build cannot draw.

    An unknown name draws nothing rather than raising: a design written by a
    later version must never make an existing report unrenderable, which is the
    same read-tolerance an unknown *kind* already gets. The write gate is where
    a typo is caught, because that is where somebody is present to be told.

    ``contenteditable="false"`` only in preview, and it is not decoration: 6c put
    a caret in the ``<h2>`` this glyph sits inside, and the attribute is what
    makes a browser treat it as one atomic thing rather than as characters to
    delete. Nothing is lost if a reader deletes it anyway — the mark lives in
    ``design``, not in the words, so the next redraw brings it back.
    """
    path = icon_path(name) if name is not None else None
    if path is None:
        return ""
    fixed = ' contenteditable="false"' if editable else ""
    # **The paint is attributes, not CSS, and that is not a style preference.**
    # WeasyPrint does not apply the HTML cascade to an inline SVG's painting, so
    # a `.cv-icon { fill: none; stroke: currentColor }` rule leaves every glyph
    # filled solid — a black disc where a clock face should be. Nothing raises
    # and no assertion about the markup can see it; it was found by looking at
    # the page. SVG presentation attributes are honoured by both renderers.
    return (
        f'<svg class="cv-icon"{fixed} viewBox="0 0 16 16" aria-hidden="true"'
        f' fill="none" stroke="{stroke}" stroke-width="1.3"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="{path}"></path></svg>'
    )


def _editable_at(index: int | None) -> str:
    """Mark the run of text a reader may rewrite. Preview only — ``None`` is print.

    Two attributes rather than one: ``data-block-text`` says *which block's*
    override this text is, and ``contenteditable`` is what lets a caret into it.
    The canvas needs both and neither belongs on a printed page.

    **Which kinds get this is decided by ``TEXT_KINDS``, the same frozenset
    ``validate_blocks`` refuses a ``text`` override outside of.** One source of
    truth, so the sheet cannot offer an edit the write gate would answer with a
    422 the reader has no way to see the cause of.
    """
    if index is None:
        return ""
    return f' data-block-text="{index}" contenteditable="true"'


def _cover_html(
    title: str,
    *,
    language: str,
    report_date: datetime.date | None,
    brand: CoverBrand,
    assets: Mapping[uuid.UUID, ReportAssetRef],
    at: str = "",
    edit: str = "",
) -> str:
    """The first page: what the report is, when it was made, and whose mark it wears.

    Deliberately not the prompt — printing it was a step-2 defect, where it
    appeared as the heading and again as body text beneath it.

    The logo and the sign-off lines are opt-in, which is what the original
    version of this docstring promised: furniture a reader cannot remove is
    worse than none. With a default ``CoverBrand`` this renders exactly the
    markup it rendered before 5f, character for character.

    An unrecognised ``variant`` renders the full cover rather than raising: a
    design written by a later version must never make an existing report
    unrenderable. That fallback is the ``if``/``if``/return shape below, not a
    normalising line — an earlier version normalised the value first, and a
    mutation showed the line could be deleted with every test still green,
    because the structure already did the work.
    """
    eyebrow = f'<p class="eyebrow">{_text(label("report_eyebrow", language))}</p>'
    # Only the ``<h1>`` opens to a caret. The cover is also an eyebrow, a date and
    # two signature lines; reading the block's own text content would fold all of
    # them into the override the first time anyone typed.
    heading = f"<h1{edit}>{_text(title)}</h1>"

    if brand.variant == "minimal":
        # Title and rule only. A minimal cover that still printed a date and two
        # signature lines would not be one.
        return f'<div class="cover minimal"{at}>{eyebrow}{heading}<div class="rule"></div></div>'

    logo = ""
    asset = assets.get(brand.logo_asset_id) if brand.logo_asset_id is not None else None
    if asset is not None:
        encoded = base64.b64encode(asset.data).decode("ascii")
        logo = f'<img class="logo" src="data:{_text(asset.mime)};base64,{encoded}" alt="">'

    meta = ""
    if report_date is not None:
        stamp = report_date.strftime("%d.%m.%Y")
        meta = f'<p class="meta">{_text(label("prepared", language))}: {_text(stamp)}</p>'

    approval = ""
    if brand.approval:
        approval = (
            '<dl class="approval">'
            f"<div><dt>{_text(label('prepared_by', language))}</dt><dd></dd></div>"
            f"<div><dt>{_text(label('approved_by', language))}</dt><dd></dd></div>"
            "</dl>"
        )

    if brand.variant == "band":
        return (
            f'<div class="cover band"{at}>'
            f'<div class="banner">{eyebrow}{heading}</div>'
            f"{logo}{meta}{approval}</div>"
        )

    # ``full`` — and the pre-5f markup exactly, when the brand is default.
    return (
        f'<div class="cover"{at}>{logo}{eyebrow}{heading}'
        f'<div class="rule"></div>{meta}{approval}</div>'
    )


def _error_body(section: SectionResult, *, language: str, at: str = "") -> str:
    return (
        f"<p{at}>{_text(label('section_failed', language))}</p>"
        f'<p class="note">{_text(short_error(section.error))}</p>'
    )


def _sources_body(
    section: SectionResult, override: str | None, at: str = "", edit: str = ""
) -> str:
    """The citations line — the reader's version of it, or the resolved one.

    The label stays either way: it is the block's furniture rather than its
    content, and a reader editing the list is not asking for the word "Sources"
    to disappear. An override does not touch ``section.sources``, so the numbers
    a prose ``[1]`` refers to are the same ones before and after the edit.

    **That furniture is why this is the one place preview mode adds an element.**
    Every other editable kind is an element whose whole content is the override;
    here the label shares the paragraph with it, so the editable run needs a box
    of its own or the first keystroke would fold "Джерела / Sources:" into the
    stored text. An inline ``<span>`` inside a ``<p>`` cannot do what the
    no-elements rule was written to prevent — that rule is about block boxes and
    ``column-span: all`` — and it is stripped by the additive-identity test along
    with the attributes.
    """
    listed = (
        _text(override)
        if override is not None
        else " · ".join(f"[{ref.number}] {_text(ref.title)}" for ref in section.sources)
    )
    run = f"<span{edit}>{listed}</span>" if edit else listed
    return f'<p class="sources"{at}>Джерела / Sources: {run}</p>'


def _table_body(section: SectionResult, *, language: str, at: str = "") -> str:
    """The table, its "no rows" replacement, and the trailing omitted-rows note.

    All three belong to one block: they are the same statement about the same
    result, and separating them would let a reader delete the table and keep the
    note that explains what the table left out.
    """
    result = section.result
    if result is None:
        return ""
    # #204: which databases contributed nothing. Printed on the empty path too —
    # a merged section that found nothing everywhere is exactly where the reader
    # most needs to know it was asking several databases.
    notes = "".join(f'<p class="note merge-note">{_text(note)}</p>' for note in section.notes)
    if not result.rows:
        return f"<p{at}>{_text(label('no_rows', language))}</p>{notes}"
    labels = section.column_labels or {}
    out = _table_html(result, language=language, labels=labels, at=at)
    if section.overlap_warning is not None:
        out = f'<p class="caveat">{_text(label("overlapping_periods", language))}</p>' + out
    out += notes
    omitted = max(len(result.rows) - _MAX_TABLE_ROWS, 0)
    if omitted:
        out += f'<p class="note">{_text(rows_note("more_rows", omitted, language))}</p>'
    return out


def _image_html(block: Block, assets: Mapping[uuid.UUID, ReportAssetRef], at: str = "") -> str:
    """An ``image`` block, or the empty-state box when it names no usable asset.

    A ``data:`` URI rather than a temporary file: WeasyPrint reads it without
    touching the filesystem, so a render never depends on a writable temp
    directory on the dyno.

    A design can outlive its picture — assets are deleted explicitly — so a
    reference that no longer resolves renders the empty state rather than
    raising. The tests assert the ``<img>`` is *absent*, not merely that nothing
    raised: WeasyPrint drops an image it cannot parse and renders everything
    else, which is how a complete-looking, pictureless document gets shipped.
    """
    asset = assets.get(block.asset_id) if block.asset_id is not None else None
    if asset is None:
        return f'<div class="img-empty"{at}></div>'
    encoded = base64.b64encode(asset.data).decode("ascii")
    width = block.width if block.width is not None else 70
    align = block.align if block.align in ("left", "center", "right") else "center"
    caption = f"<figcaption>{_text(block.caption)}</figcaption>" if block.caption else ""
    return (
        f'<figure class="img-block {align}"{at}>'
        f'<img src="data:{_text(asset.mime)};base64,{encoded}" style="width:{width}%" alt="">'
        f"{caption}</figure>"
    )


def _toc_entries(blocks: list[Block], sections: list[SectionResult]) -> list[tuple[int, str]]:
    """(section index, title) for every heading the document actually prints.

    A hidden heading opens no section and prints nothing, so listing it would
    point the reader at a page that does not exist. The reader's 5e override
    wins over the model's title for the same reason it wins on the page: the
    contents must name the section the way the section names itself.
    """
    entries: list[tuple[int, str]] = []
    for block in blocks:
        if block.kind != "heading" or block.hidden or block.section is None:
            continue
        if not 0 <= block.section < len(sections):
            continue
        section = sections[block.section]
        entries.append((block.section, block.text if block.text is not None else section.title))
    return entries


def _toc_html(entries: list[tuple[int, str]], *, language: str, at: str = "") -> str:
    """The contents list. Page numbers are WeasyPrint's, via ``target-counter``.

    The number cannot be computed here — it is only known once the document has
    been laid out — so the anchor carries the question and the printer answers
    it. Rendering to HTML alone therefore shows no numbers, which is why the
    covering test asserts against a genuinely multi-page **PDF**.
    """
    items = "".join(
        f'<li><span class="toc-n">{position:02d}</span>'
        f'<a href="#sec-{index}">{_text(title)}</a></li>'
        for position, (index, title) in enumerate(entries, start=1)
    )
    return (
        f'<nav class="toc"{at}><h3>{_text(label("contents", language))}</h3><ol>{items}</ol></nav>'
    )


def _block_html(
    block: Block,
    section: SectionResult | None,
    *,
    language: str,
    assets: Mapping[uuid.UUID, ReportAssetRef],
    anchors: bool,
    index: int | None = None,
) -> str:
    """One block's markup, or "" when the block cannot be rendered.

    Returning "" rather than raising is the read half of the asymmetry: an
    unknown kind and a block naming a section that is not there both degrade to
    silence, and the rest of the document survives.

    ``index`` is the block's position in the list the document was built from,
    emitted as ``data-block-index`` for the editor's canvas to bind to. ``None``
    is print, and the attribute is the *only* difference between the two: preview
    mode adds attributes, never elements. A wrapper would have been simpler and
    is wrong — a ``<div>`` inside ``section.text-section { columns: 2 }`` also
    breaks ``column-span: all`` on the heading it would then nest.

    A block that emits two top-level elements — a table and its omitted-rows
    note, an error's two paragraphs — carries the attribute on the **first**, so
    the canvas's selection ring covers the table and not its trailing note.
    """
    at = f' data-block-index="{index}"' if index is not None else ""
    edit = _editable_at(index) if block.kind in TEXT_KINDS else ""
    if block.kind == "pagebreak":
        return f'<div class="pagebreak"{at}></div>'
    if block.kind == "callout":
        mark = _icon_html(block.icon, editable=bool(edit))
        return f'<div class="callout"{at}>{mark}<p{edit}>{_text(block.text or "")}</p></div>'
    if block.kind == "image":
        return _image_html(block, assets, at)
    if section is None:
        return ""
    if block.kind == "heading":
        # The anchor is emitted only when a toc links to it. Unconditionally
        # adding an id would change every existing document, and the goldens
        # capture the markup verbatim.
        anchor = f' id="sec-{block.section}"' if anchors and block.section is not None else ""
        title = _text(block.text if block.text is not None else section.title)
        mark = _icon_html(block.icon, editable=bool(edit))
        return f"<h2{anchor}{at}{edit}>{mark}{title}</h2>"
    if block.kind == "error":
        return _error_body(section, language=language, at=at)
    if block.kind == "prose":
        words = _text(block.text if block.text is not None else section.prose or "")
        return f"<p{at}{edit}>{words}</p>"
    if block.kind == "sources":
        # An override renders even where the section resolved no citations: the
        # reader wrote a line, and printing nothing would read as the edit having
        # been lost rather than as this section having no sources.
        if block.text is None and not section.sources:
            return ""
        return _sources_body(section, block.text, at, edit)
    if block.kind == "stats":
        if section.result is None or not section.result.rows:
            return ""
        return _stats_html(
            section.result,
            language=language,
            labels=section.column_labels or {},
            at=at,
            overlapping=section.overlap_warning is not None,
        )
    if block.kind == "viz":
        if block.variant == "table":
            return _table_body(section, language=language, at=at)
        if block.variant == "chart":
            return _chart_html(section, language=language, at=at)
    return ""


def _section_classes(section: SectionResult | None, style: ReportStyle) -> list[str]:
    """Exactly the classes ``_section_html`` chose, keyed off the heading's section.

    Derived from the heading rather than from the group's contents on purpose: it
    is what today's renderer does, and identity is the step's whole test.
    """
    classes = ["numbered"] if style.numbered_sections else []
    if section is None:
        return classes
    if section.kind == "text":
        classes.append("text-section")
    elif section.kind != "error":
        result = section.result
        if result is not None and len(result.columns) >= _LANDSCAPE_MIN_COLUMNS:
            classes.append("landscape")
    return classes


def _blocks_html(
    blocks: list[Block],
    sections: list[SectionResult],
    *,
    language: str,
    style: ReportStyle,
    title: str,
    report_date: datetime.date | None,
    assets: Mapping[uuid.UUID, ReportAssetRef],
    brand: CoverBrand,
    preview: bool = False,
) -> str:
    """The document body, with a heading opening each section and the blocks
    after it nesting inside until the next heading.

    In preview mode every block carries its **list** index — the position in
    ``blocks`` as passed in, hidden ones included. ``useBlockList`` indexes
    ``design.blocks``, so a number that skipped hidden blocks would make the tree
    and the canvas disagree about which block "block 4" is.
    """

    anchors = any(block.kind == "toc" and not block.hidden for block in blocks)

    def section_of(block: Block) -> SectionResult | None:
        if block.section is None or not 0 <= block.section < len(sections):
            return None
        return sections[block.section]

    parts: list[str] = []
    open_group: list[str] | None = None
    group_classes: list[str] = []

    def close() -> None:
        nonlocal open_group
        if open_group is None:
            return
        attribute = f' class="{" ".join(group_classes)}"' if group_classes else ""
        parts.append(f"<section{attribute}>{''.join(open_group)}</section>")
        open_group = None

    for position, block in enumerate(blocks):
        index = position if preview else None
        at = f' data-block-index="{index}"' if index is not None else ""
        # Before the grouping switch, so a hidden heading opens no section and its
        # contents join the previous group — the same document deleting it makes.
        # Hide and delete differ in reversibility, not in what they render.
        if block.hidden:
            continue
        if block.kind == "cover":
            close()
            name = block.text if block.text is not None else title
            parts.append(
                _cover_html(
                    name,
                    language=language,
                    report_date=report_date,
                    brand=brand,
                    assets=assets,
                    at=at,
                    edit=_editable_at(index),
                )
            )
            continue
        if block.kind == "toc":
            # Like the cover, a toc reads the whole block list rather than one
            # section, so it is assembled here where that list is in hand.
            close()
            parts.append(_toc_html(_toc_entries(blocks, sections), language=language, at=at))
            continue
        if block.kind == "heading":
            section = section_of(block)
            if section is None:
                continue
            close()
            group_classes = _section_classes(section, style)
            open_group = [
                _block_html(
                    block,
                    section,
                    language=language,
                    assets=assets,
                    anchors=anchors,
                    index=index,
                )
            ]
            continue
        markup = _block_html(
            block,
            section_of(block),
            language=language,
            assets=assets,
            anchors=anchors,
            index=index,
        )
        if not markup:
            continue
        if open_group is None:
            parts.append(markup)
        else:
            open_group.append(markup)
    close()
    body = "".join(parts)
    if not preview:
        return body
    # What `@page`'s margin boxes draw on every sheet, drawn once on the canvas's
    # single continuous one. The footer carries the word and a dash and never a
    # number: the canvas cannot paginate, and an invented page count would be a
    # lie in a filed document's editor (spec decision 3).
    #
    # The header is dropped when the document opens on a cover — exactly what
    # `@page :first { @top-left { content: none } }` does for the printed page,
    # and for the same reason its comment gives: otherwise the title appears
    # twice near the top, once large and once small. Found by putting the canvas
    # and the PDF side by side; no assertion had anything to say about it.
    visible = [block.kind for block in blocks if not block.hidden]
    head = (
        ""
        if visible[:1] == ["cover"]
        else f'<header class="cv-head"><span>{_text(title)}</span></header>'
    )
    foot = f'<footer class="cv-foot"><span>{_text(label("page", language))} —</span></footer>'
    return f'<div class="cv-sheet">{head}{body}{foot}</div>'


def build_report_html(
    *,
    title: str,
    sections: list[SectionResult],
    language: str = "en",
    style: ReportStyle | None = None,
    report_date: datetime.date | None = None,
    blocks: list[Block] | None = None,
    assets: Mapping[uuid.UUID, ReportAssetRef] | None = None,
    brand: CoverBrand | None = None,
    preview: bool = False,
) -> str:
    """The whole report as one self-contained HTML document.

    The reader's prompt is deliberately not printed: it used to be rendered as the
    heading *and* again as body text directly beneath it. It is still on the
    report row and in the history list, where it belongs.

    Citation numbering is per-section (each section's ``[n]`` markers index its
    own retrieved chunks), so each text section renders its own numbered source
    list — there is deliberately no global sources section.

    ``style`` defaults to the repository template rather than raising: every
    caller predating step 3a passes none, and step 3b will pass the one stored on
    the report row.

    ``blocks`` is the ordered list the document is assembled from. ``None`` means
    the report is unedited and the list is derived here — deliberately here and
    not earlier, because ``chart_svg`` is populated immediately before this call
    and the chart block's presence depends on it.

    ``preview`` is the design canvas's document rather than the printed one, and
    it **defaults to the exact identity**: the eleven byte-identity goldens
    capture this function's whole output including its ``<style>``, so anything
    emitted unconditionally would move every one of them and silently restyle
    ~470 stored reports. Spec decision 2, and 5f's discipline exactly.
    """
    style = style or resolve_style(None)
    block_list = blocks if blocks is not None else default_blocks(sections)
    body = _blocks_html(
        block_list,
        sections,
        language=language,
        style=style,
        title=title,
        report_date=report_date,
        assets=assets or {},
        brand=brand or CoverBrand(),
        preview=preview,
    )
    body_class = ' class="rules-on"' if style.section_rules else ""
    # Keyed off the caveat actually being in the body, not off the sections list:
    # the caveat rides with the ``viz/table`` block, and a stored design is free
    # to drop that block while keeping the section. Asking the finished markup is
    # the one answer that cannot drift from ``_table_body``'s own branches — the
    # same discipline ``_ICON_CSS`` follows by asking whether a mark resolved
    # rather than whether a field was set.
    has_caveat = 'class="caveat"' in body
    conditional_css = _conditional_css(
        block_list, style, brand or CoverBrand(), preview=preview, has_caveat=has_caveat
    )
    return (
        "<!doctype html>"
        f'<html lang="{_text(language)}"><head><meta charset="utf-8">'
        f"<title>{_text(document_title(title, block_list))}</title>"
        f"<style>{stylesheet(style)}{_page_css(style, language)}"
        f"{conditional_css}"
        f"{(_preview_font_css(style) + _preview_css(style)) if preview else ''}</style></head>"
        f"<body{body_class}>{body}</body></html>"
    )


def _conditional_css(
    blocks: list[Block],
    style: ReportStyle,
    brand: CoverBrand,
    *,
    preview: bool = False,
    has_caveat: bool = False,
) -> str:
    """Rules for the 5f block kinds, emitted only where they are used.

    Not tidiness: the eleven byte-identity goldens capture the document's whole
    ``<style>`` block, so an unconditional addition here would move every one of
    them — and a golden that moves for a feature no golden exercises is a
    silent restyle of every stored report, not a passing test to regenerate.
    """
    visible = [block for block in blocks if not block.hidden]
    kinds = {block.kind for block in visible}
    branded = brand != CoverBrand()
    toc_css = _TOC_CSS_PREVIEW if preview else _TOC_CSS
    return (
        (toc_css if "toc" in kinds else "")
        + (_IMAGE_CSS if "image" in kinds else "")
        + (_CAVEAT_CSS if has_caveat else "")
        + (_COVER_CSS if branded else "")
        # Keyed off the variant, not off `branded`: a stored design that picked
        # any other cover must keep the document it already renders, and the
        # goldens that exercise a branded cover exercise no band.
        + (_band_cover_css(style) if brand.variant == "band" else "")
        + (_RULES_CSS if style.section_rules else "")
        # Keyed off a mark actually being drawn, not off the field being present:
        # `icon="none"` and a name this build cannot draw both render nothing, and
        # a rule for a glyph that is not there is a golden moved for nothing.
        + (_ICON_CSS if any(icon_path(block.icon or "") for block in visible) else "")
    )
