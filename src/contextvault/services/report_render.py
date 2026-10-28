"""Turn a report document into PDF bytes (WeasyPrint).

This module is now only the printer. The document itself — every heading, table
and figure — is built by ``report_html`` as a string, which is where the layout
decisions and their tests live.

Cyrillic is a hard requirement, and it is met by declaring the font file rather
than naming a family. fontconfig cannot see a font packaged inside a Python
wheel, so an ``@font-face`` pointing at that file is what makes a developer's
Mac and the dyno render the same typeface. **Which file backs a family lives in
``report_fonts``**, because the design canvas serves the same faces over HTTP and
a second answer to that question would be a canvas in a different typeface from
the page it previews.

What used to be here — measuring columns, wrapping a wide table into blocks,
fitting text to a cell with an ellipsis, placing an image against the remaining
page height — is gone. CSS does that job, and the arithmetic was only ever
compensating for fpdf2 not clipping.
"""

import datetime
import uuid
from collections.abc import Mapping

# WeasyPrint ships no stubs and no py.typed marker. The ignore is per-import
# rather than a project-wide ignore_missing_imports, which would also silence a
# genuinely missing dependency.
from weasyprint import CSS, HTML  # type: ignore[import-untyped]
from weasyprint.text.fonts import FontConfiguration  # type: ignore[import-untyped]

from contextvault.services.report_blocks import Block
from contextvault.services.report_fonts import font_path
from contextvault.services.report_html import CoverBrand, build_report_html
from contextvault.services.report_style import SANS, ReportStyle, resolve_style
from contextvault.services.report_types import ReportAssetRef, SectionResult

__all__ = ["PAPER_FONT", "build_report_pdf", "render_pdf"]

# The family names are ours rather than "DejaVu Sans" so a stylesheet asks for a
# font we define, not for whatever the host happens to call DejaVu — the dyno, a
# developer's Mac and a reader's browser do not agree on that.
PAPER_FONT = SANS


def _font_css(style: ReportStyle) -> str:
    """An ``@font-face`` per family and weight the style actually uses.

    Both weights are declared for every family, and that is not belt-and-braces.
    Measured against WeasyPrint 69: with only a 400 face declared, the PDF still
    embeds a font *named* ``<Family>-Bold``, but its program is a synthesised
    emboldening of the regular outlines rather than the real bold file. So the
    embedded-font-name assertion passes while the page shows a fake bold; only
    the bytes differ.
    """
    rules = []
    for family in sorted({style.body_family, style.display_family, style.numeral_family}):
        for weight in (400, 700):
            path = font_path(family, weight)
            rules.append(
                f'@font-face {{ font-family: "{family}"; font-weight: {weight};'
                f' src: url("file://{path}"); }}'
            )
    return "".join(rules)


def _render_with_css(html: str, font_css: str) -> bytes:
    """The printer proper, with the font rules handed in.

    Split out from ``render_pdf`` so a test can render the same document with a
    deliberately incomplete set of faces — which is the only way to show that
    declaring weight 700 changes what lands on the page.

    ``font_config`` is passed to *both* calls on purpose. WeasyPrint registers
    ``@font-face`` sources only on the configuration handed to it, so giving it to
    ``CSS`` alone leaves ``write_pdf`` rendering with whatever fontconfig offers —
    silently, with no warning and no exception. The failure looks like a working
    report in the wrong typeface, which no test asserting on text can see.
    """
    font_config = FontConfiguration()
    sheet = CSS(string=font_css, font_config=font_config)
    return bytes(HTML(string=html).write_pdf(stylesheets=[sheet], font_config=font_config))


def render_pdf(html: str, *, style: ReportStyle | None = None) -> bytes:
    """A self-contained HTML document to PDF bytes, in the template's faces."""
    return _render_with_css(html, _font_css(style or resolve_style(None)))


def build_report_pdf(
    *,
    title: str,
    sections: list[SectionResult],
    language: str = "en",
    style: ReportStyle | None = None,
    report_date: datetime.date | None = None,
    blocks: list[Block] | None = None,
    assets: Mapping[uuid.UUID, ReportAssetRef] | None = None,
    brand: CoverBrand | None = None,
) -> bytes:
    """The blended-report PDF: the document from ``report_html``, printed.

    ``report_date`` is the report row's creation date, not today's: a re-render
    must reproduce the cover it had, and "today" would quietly restamp it.
    """
    style = style or resolve_style(None)
    return render_pdf(
        build_report_html(
            title=title,
            sections=sections,
            language=language,
            style=style,
            report_date=report_date,
            blocks=blocks,
            assets=assets,
            brand=brand,
        ),
        style=style,
    )
