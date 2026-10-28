"""PDF assembly — the font guard, and that the document survives the printer.

Everything about *what the document says* is asserted in ``test_report_html``
against the markup. What is left here can only be checked on the PDF itself: the
font it embeds, and that the text and the page furniture come out the other side.

Deliberately absent, and not an oversight: the three tests that spied on
``FPDF.cell`` to prove drawn text was narrower than the cell it went into
(``…never_paints_text_outside_its_cell``, ``…is_clipped_rather_than_painted_over``,
``…still_shows_its_values_in_full``). They guarded fpdf2 not clipping — a cell
handed forty characters painted over its neighbour. CSS clips at the cell edge
and wraps inside it, so that defect has no form here; what remains worth
guarding, that no column is silently dropped and no value is silently truncated,
is asserted in ``test_report_html`` instead.
"""

import datetime
import uuid
from decimal import Decimal
from io import BytesIO

import pytest
from pypdf import PdfReader

from contextvault.services.report_blocks import Block
from contextvault.services.report_chart import render_chart_images
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_html import CoverBrand
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_render import (
    PAPER_FONT,
    _font_css,
    _render_with_css,
    build_report_pdf,
    render_pdf,
)
from contextvault.services.report_style import TEMPLATES
from contextvault.services.report_types import SectionResult, SectionSourceRef

RESULT = QueryResult(columns=["city", "revenue"], rows=[("Київ", 120), ("Львів", 80)])


def _data_section(result: QueryResult, chart_svg: bytes | None) -> SectionResult:
    return SectionResult(
        title="Дохід за містами",
        source="database",
        sub_question="дохід",
        kind="data",
        result=result,
        chart_svg=chart_svg,
        generated_sql="SELECT 1",
        chart_spec={"chart_type": "bar"},
    )


def _embedded_fonts(pdf: bytes) -> set[str]:
    """The BaseFont name of every font the PDF actually embeds.

    Subset prefixes ("/WWFCOD+Report-Sans") are kept: the prefix is what shows the
    font was subsetted out of a file rather than merely named.
    """
    names: set[str] = set()
    for page in PdfReader(BytesIO(pdf)).pages:
        fonts = (page.get("/Resources", {}) or {}).get("/Font") or {}
        for value in fonts.values():
            names.add(str(value.get_object().get("/BaseFont", "")))
    return names


def _pdf_text(pdf: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages)


def test_the_pdf_embeds_our_own_font_rather_than_a_system_substitute() -> None:
    """WeasyPrint substitutes silently when ``@font-face`` is not registered.

    On an image carrying no DejaVu the spike's document came out in Liberation
    Mono — no exception, no warning, perfectly legible Cyrillic. So "the PDF
    contains Cyrillic" passes straight through the defect, and the font's *name*
    is the only thing that separates the two outcomes.
    """
    pdf = render_pdf(
        f'<style>body {{ font-family: "{PAPER_FONT}"; }}</style>'
        "<p>Дохід за містами</p><p><b>Підсумок</b></p>"
    )
    embedded = _embedded_fonts(pdf)
    family = PAPER_FONT.replace(" ", "-")
    assert any(family in name for name in embedded), embedded


def test_the_pdf_carries_the_sections_text_and_the_localised_page_footer() -> None:
    text_section = SectionResult(
        title="Політика відпусток",
        source="documents",
        sub_question="правила",
        kind="text",
        prose="Відпустка триває 24 дні [1].",
        sources=[SectionSourceRef(number=1, title="policy.pdf", source_id=uuid.uuid4())],
    )
    pdf = build_report_pdf(
        title="Квартальний звіт",
        sections=[_data_section(RESULT, None), text_section],
        language="uk",
    )
    assert pdf[:5] == b"%PDF-"
    text = _pdf_text(pdf)
    assert "Квартальний звіт" in text
    assert "Дохід за містами" in text
    assert "Київ" in text
    assert "Стор." in text  # the footer, in the report's language, not "Page"


def _page_drawing_bytes(pdf: bytes) -> int:
    """Drawing operations across the whole document, not the first page.

    It read ``pages[0]`` until the cover landed there in step 3a and pushed every
    section onto page 2 — at which point the measurement was of a blank sheet and
    the assertion failed against perfectly correct output.
    """
    total = 0
    for page in PdfReader(BytesIO(pdf)).pages:
        contents = page.get_contents()
        assert contents is not None, "a page has no content stream at all"
        total += len(contents.get_data())
    return total


def test_the_svg_chart_actually_reaches_the_page() -> None:
    """WeasyPrint drops an image it cannot parse and renders everything else.

    So a broken SVG produces a valid, complete-looking report with no chart in it
    — nothing raises and the PDF is well-formed. The only way to see it is to
    compare against the same report built without the chart.

    Asserting on the page's XObject resources does not work here: WeasyPrint
    draws an SVG as vector operations straight into the content stream rather
    than embedding it as an image object. That was checked, not assumed.
    """
    images = render_chart_images(
        RESULT,
        ChartSpec(chart_type="bar", x_column="city", y_column="revenue", title="Дохід"),
        language="uk",
    )
    assert images is not None
    without = build_report_pdf(title="Звіт", sections=[_data_section(RESULT, None)], language="uk")
    with_chart = build_report_pdf(
        title="Звіт", sections=[_data_section(RESULT, images.svg)], language="uk"
    )
    drawn = _page_drawing_bytes(with_chart) - _page_drawing_bytes(without)
    assert drawn > 5_000, f"the chart added only {drawn} bytes of drawing operations"


def test_pdf_builds_with_a_single_data_section_and_an_empty_result() -> None:
    empty = QueryResult(columns=["city"], rows=[])
    pdf = build_report_pdf(title="Report", sections=[_data_section(empty, None)])
    assert pdf[:5] == b"%PDF-"
    assert "no data" in _pdf_text(pdf).lower() or "holds no data" in _pdf_text(pdf)


def test_pdf_builds_with_a_bool_column() -> None:
    result = QueryResult(columns=["city", "active"], rows=[("Kyiv", True), ("Lviv", False)])
    pdf = build_report_pdf(title="Report", sections=[_data_section(result, None)])
    assert pdf[:5] == b"%PDF-"


def test_a_failed_section_prints_a_short_note_not_a_stack_trace() -> None:
    failed = SectionResult(
        title="Зламана секція",
        source="database",
        sub_question="q",
        kind="error",
        error="relation does not exist",
    )
    text = _pdf_text(build_report_pdf(title="Звіт", sections=[failed], language="uk"))
    assert "не вдалося сформувати" in text
    assert "could not be generated" not in text


def test_a_large_decimal_reaches_the_page_without_scientific_notation() -> None:
    """The "3.6498000E+11" defect, checked on the printed page rather than the markup."""
    result = QueryResult(columns=["fiscal_year", "value"], rows=[(2024, Decimal("3.6498000E+11"))])
    text = _pdf_text(
        build_report_pdf(title="Звіт", sections=[_data_section(result, None)], language="uk")
    )
    assert "E+" not in text
    assert "млрд" in text


# --- the template reaches the page ---------------------------------------------


def _font_programs(pdf: bytes) -> dict[str, int]:
    """BaseFont name -> length of the embedded font program.

    The name alone cannot answer the question this file needs answered twice: a
    substituted font and a fabricated bold both keep a plausible name.
    """
    out: dict[str, int] = {}
    for page in PdfReader(BytesIO(pdf)).pages:
        for value in ((page.get("/Resources", {}) or {}).get("/Font") or {}).values():
            font = value.get_object()
            descendants = font.get("/DescendantFonts")
            descriptor = descendants[0].get_object().get("/FontDescriptor") if descendants else None
            file_ref = descriptor.get("/FontFile2") if descriptor else None
            out[str(font.get("/BaseFont", ""))] = (
                len(file_ref.get_data()) if file_ref is not None else -1
            )
    return out


def test_the_bold_on_the_page_is_the_real_bold_file_not_a_fabricated_one() -> None:
    """Declaring only a 400 face does not fail loudly — it fakes the bold.

    WeasyPrint still embeds a font *named* "<Family>-Bold", so the embedded-name
    assertion above passes either way; only the program bytes differ (measured:
    8368 fabricated against 8140 real). This asserts the weight-700 declaration
    changes what lands on the page.
    """
    style = TEMPLATES["chasopys"]  # a serif template: the fake is a serif fake
    document = (
        f'<style>body {{ font-family: "{style.body_family}"; }}</style><p><b>Підсумок</b></p>'
    )
    real = _font_programs(render_pdf(document, style=style))
    without_bold = "@font-face".join(
        part for part in _font_css(style).split("@font-face") if "font-weight: 700" not in part
    )
    fabricated = _font_programs(_render_with_css(document, without_bold))
    assert real, "no font was embedded at all"
    assert sorted(real.values()) != sorted(fabricated.values())


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_every_template_produces_a_pdf_carrying_its_text(key: str) -> None:
    pdf = build_report_pdf(
        title="Квартальний звіт",
        sections=[_data_section(RESULT, None)],
        language="uk",
        style=TEMPLATES[key],
    )
    assert pdf[:5] == b"%PDF-"
    assert "Київ" in _pdf_text(pdf)


def test_the_serif_template_embeds_the_serif_face() -> None:
    """The families are ours, so a substitution keeps the name — but not the file."""
    pdf = build_report_pdf(
        title="Звіт",
        sections=[_data_section(RESULT, None)],
        language="uk",
        style=TEMPLATES["chasopys"],
    )
    assert any("Report-Serif" in name for name in _embedded_fonts(pdf)), _embedded_fonts(pdf)


def test_the_cover_stands_alone_and_the_header_names_the_document_after_it() -> None:
    """`@page :first` and `string-set` are where this can silently do nothing.

    A `@page :first` rule that fails to match leaves the title printed twice on
    the cover, large and small, three centimetres apart; a `string-set` that
    never resolves leaves interior pages bare. Neither raises, and neither is
    visible in an assertion about the document's text as a whole.
    """
    sections = [_data_section(RESULT, None) for _ in range(4)]
    pdf = build_report_pdf(
        title="Квартальний звіт",
        sections=sections,
        language="uk",
        report_date=datetime.date(2026, 8, 4),
    )
    pages = [page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages]
    assert len(pages) >= 2, "the cover did not force a page break"
    assert "04.08.2026" in pages[0]
    assert "Київ" not in pages[0], "the cover must carry no section content"
    assert pages[0].count("Квартальний звіт") == 1, "the header printed on the cover too"
    assert all("Квартальний звіт" in page for page in pages[1:]), "the header is missing"


def test_the_wide_section_really_lands_on_a_landscape_page() -> None:
    """A named page no element claims silently changes nothing at all."""
    wide = QueryResult(columns=[f"col_{index}" for index in range(12)], rows=[tuple(range(12))])
    pdf = build_report_pdf(title="Звіт", sections=[_data_section(wide, None)], language="uk")
    sizes = [
        (float(page.mediabox.width), float(page.mediabox.height))
        for page in PdfReader(BytesIO(pdf)).pages
    ]
    assert any(width > height for width, height in sizes), sizes
    assert sizes[0][1] > sizes[0][0], "the cover must stay portrait"


def _rightmost_ink(pdf: bytes, *, scale: float = 1.1) -> list[tuple[int, int, int]]:
    """Per page: (page number, rightmost inked column, the content edge).

    Rasterised because this is the one defect class the text has no opinion
    about: a statistic that ran off the right edge of the sheet is extracted in
    full, in order, exactly as if it had fitted.
    """
    # A dev-only dependency, imported here rather than at module scope, and
    # untyped like WeasyPrint — the ignore is per-import for the same reason.
    import pypdfium2  # type: ignore[import-untyped]

    out = []
    document = pypdfium2.PdfDocument(BytesIO(pdf))
    for index in range(len(document)):
        bitmap = document[index].render(scale=scale).to_numpy()
        height, width = bitmap.shape[:2]
        paper = bitmap[2, 2][:3]
        inked = [column for column in range(width) if (bitmap[:, column, :3] != paper).any()]
        out.append((index + 1, max(inked) if inked else 0, width))
    return out


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_nothing_is_printed_past_the_right_margin(key: str) -> None:
    """Found by looking at Президія, where the statistics line ran off the sheet.

    Its type is 12pt against Кварта's 10, and the summary spans were joined with
    no whitespace — so the line had no legal break point and simply overflowed.
    Every text assertion passed while it did.
    """
    style = TEMPLATES[key]
    result = QueryResult(
        columns=["quarter", "revenue", "volume", "margin"],
        rows=[
            (f"2024-Q{index}", Decimal("364980000000"), 12_400 + index, Decimal("18.5"))
            for index in range(1, 5)
        ],
    )
    labels = {
        "quarter": "Квартал",
        "revenue": "Виторг, ₴",
        "volume": "Обсяг, шт.",
        "margin": "Маржа, %",
    }
    pdf = build_report_pdf(
        title="Квартальний огляд ринку",
        sections=[
            SectionResult(
                title="Ринкова динаміка",
                source="database",
                sub_question="q",
                kind="data",
                result=result,
                column_labels=labels,
            )
        ],
        language="uk",
        style=style,
    )
    edge = style.page_margin_mm[1] * 72 / 25.4 * 1.1
    for page, rightmost, width in _rightmost_ink(pdf):
        assert rightmost <= width - edge + 2, (
            f"{style.name} page {page}: ink reaches {rightmost}, content ends at {width - edge:.0f}"
        )


def test_the_section_after_a_wide_one_returns_to_portrait() -> None:
    """Otherwise it is set on the turned sheet, at a 27 cm measure.

    Found by looking at the rendered pages: the error section following a
    twelve-column table was printed on the landscape sheet. Prose there would be
    one line across the whole width, which is unreadable and which no assertion
    about page sizes alone would notice.
    """
    wide = QueryResult(columns=[f"col_{index}" for index in range(12)], rows=[tuple(range(12))])
    after = SectionResult(
        title="Наступна секція",
        source="documents",
        sub_question="q",
        kind="text",
        prose="Текст, який має читатися у книжковій орієнтації. " * 12,
    )
    pdf = build_report_pdf(title="Звіт", sections=[_data_section(wide, None), after], language="uk")
    reader = PdfReader(BytesIO(pdf))
    orientation = [
        "landscape" if float(page.mediabox.width) > float(page.mediabox.height) else "portrait"
        for page in reader.pages
    ]
    text = [page.extract_text() or "" for page in reader.pages]
    following = next(index for index, body in enumerate(text) if "Наступна секція" in body)
    assert orientation[following] == "portrait", orientation


def _many_section_report(count: int) -> tuple[str, list[SectionResult]]:
    """Enough sections to force the document past one page.

    A single-page fixture cannot test ``target-counter`` at all: every entry
    would legitimately read "1", so a renderer where the rule never resolved
    would still pass. Titles are lettered rather than numbered on purpose — a
    digit in the title is indistinguishable from a page number once the PDF is
    flattened to text, and an earlier version of this test read the section
    index out of "Розділ 9" and called it a page number.
    """
    rows = [(f"Місто {n}", n * 1000) for n in range(12)]
    result = QueryResult(columns=["city", "revenue"], rows=rows)
    sections = [
        SectionResult(
            title=f"Розділ {chr(ord('а') + n)}",
            source="database",
            sub_question="питання",
            kind="data",
            result=result,
        )
        for n in range(count)
    ]
    return "Багатосторінковий звіт", sections


def _toc_page_numbers(pdf: bytes) -> list[int]:
    """The page numbers ``target-counter`` printed against the contents entries.

    They are read off the **contents page alone**, as bare integer lines: the
    rule sets them with ``float: right``, so the flattened text carries each one
    as its own run, separate from the entry it belongs to. Nothing else on that
    page extracts as a bare integer — the entry numbering is glued to the title
    ("01Розділ а") and the footer reads "Стор. 1 / 5".
    """
    first_page = PdfReader(BytesIO(pdf)).pages[0].extract_text() or ""
    return [int(line.strip()) for line in first_page.splitlines() if line.strip().isdigit()]


def test_a_toc_prints_real_page_numbers_not_all_ones() -> None:
    """``target-counter`` is the only thing that can answer "which page".

    The number is unknowable at markup time — it exists only once WeasyPrint has
    laid the document out — so this is asserted on a genuinely multi-page PDF,
    on extracted text rather than raw bytes. A byte-level assertion would pass
    regardless: both renderers compress their content streams.
    """
    section_count = 10
    title, sections = _many_section_report(section_count)
    blocks = [Block(kind="toc")] + [
        block
        for index in range(len(sections))
        for block in (
            Block(kind="heading", section=index),
            Block(kind="viz", section=index, variant="table"),
        )
    ]
    pdf = build_report_pdf(title=title, sections=sections, language="uk", blocks=blocks)

    assert len(PdfReader(BytesIO(pdf)).pages) > 1, "fixture must span more than one page"
    numbers = _toc_page_numbers(pdf)
    assert len(numbers) == section_count, (
        f"expected one page number per contents entry, got {numbers}"
    )
    assert max(numbers) > 1, (
        f"every contents entry claims page 1 — target-counter did not resolve: {numbers}"
    )


def test_a_cover_edited_on_the_sheet_renames_the_printed_running_header() -> None:
    """Step 6c's one acceptance criterion that only the PDF can answer.

    The header is not a second field: ``string-set: doctitle`` publishes the
    cover's own ``<h1>`` into ``@page { @top-left }``. 6c put a caret in that
    ``<h1>``, and the question that raises is whether the words a reader types
    there still reach every interior page — which no assertion about the canvas
    can see, because the canvas draws its header once and does not paginate.

    Asserted here rather than trusted to print being byte-identical: identity is
    a claim about *this* renderer's output, and this is the property a reader
    would check.
    """
    blocks = [Block(kind="cover", text="Квартальний звіт, виправлено")]
    for index in range(4):
        blocks += [Block(kind="heading", section=index), Block(kind="viz", section=index)]
    pdf = build_report_pdf(
        title="Згенерована назва",
        sections=[_data_section(RESULT, None) for _ in range(4)],
        language="uk",
        blocks=blocks,
    )

    pages = [page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages]
    assert len(pages) >= 2, "the cover did not force a page break"
    assert all("Квартальний звіт, виправлено" in page for page in pages[1:])
    # The generated name is gone from the document, not merely covered on page one.
    assert not any("Згенерована назва" in page for page in pages)


def _cover_band_edges(
    pdf: bytes, *, scale: float = 1.6
) -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    """Page one's (left edge, right edge, paper) colours across the title band.

    Rasterised for the same reason ``_rightmost_ink`` is: a background is exactly
    what extracted text has no opinion about. A ``.banner`` that paints nothing
    emits the same markup, the same words and the same page count as one that
    paints, which is how #173's variant shipped drawing no band at all.

    The row is sampled at 10 % of the page height — below the sheet's top edge
    and well inside the title block on every template — and paper is sampled from
    the bottom-left corner, which no cover variant paints.
    """
    # Dev-only, so imported in the function as ``_rightmost_ink`` does. No
    # ``type: ignore`` here: that import already carries the one mypy wants for
    # this untyped module, and a second is reported as unused.
    import pypdfium2

    document = pypdfium2.PdfDocument(BytesIO(pdf))
    bitmap = document[0].render(scale=scale).to_numpy()
    height, width = bitmap.shape[:2]
    row = int(height * 0.10)
    return (
        tuple(int(channel) for channel in bitmap[row, 0][:3]),
        tuple(int(channel) for channel in bitmap[row, width - 1][:3]),
        tuple(int(channel) for channel in bitmap[height - 3, 2][:3]),
    )


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_the_band_cover_paints_a_band_across_the_full_sheet(key: str) -> None:
    """#173: the variant was named ``band`` and drew none.

    ``.cover.band .banner`` carried padding and margin only — no fill anywhere in
    ``src`` — so the variant differed from ``full`` by nothing but the omitted
    rule and a 34 mm pull-up. Measured before the fix: the left margin over the
    top 40 % of the page held **exactly one** colour, the paper, on both Кварта
    and Президія, and the band/full difference box stopped at the content edge.

    Asserting both edges is what makes this a *band* rather than a tinted box:
    the fill has to survive the page margin on both sides.
    """
    pdf = build_report_pdf(
        title="Квартальний звіт",
        sections=[_data_section(RESULT, None)],
        language="uk",
        style=TEMPLATES[key],
        brand=CoverBrand(variant="band"),
    )
    left, right, paper = _cover_band_edges(pdf)
    assert left != paper, f"{key}: the band does not reach the left edge of the sheet"
    assert right != paper, f"{key}: the band does not reach the right edge of the sheet"
    assert left == right, f"{key}: the band is two edges, not one continuous field"


@pytest.mark.parametrize("key", sorted(TEMPLATES))
def test_a_branded_cover_that_is_not_the_band_variant_paints_no_band(key: str) -> None:
    """The other half of the guard: the fill is keyed to the variant, not to branding.

    ``approval=True`` makes this brand non-default, so the cover CSS *is* emitted
    — an unconditional band rule would therefore paint here, restyling every
    stored branded report and moving goldens that exercise no band at all.
    """
    pdf = build_report_pdf(
        title="Квартальний звіт",
        sections=[_data_section(RESULT, None)],
        language="uk",
        style=TEMPLATES[key],
        brand=CoverBrand(variant="full", approval=True),
    )
    left, right, paper = _cover_band_edges(pdf)
    assert left == paper, f"{key}: a full cover painted something at the left edge"
    assert right == paper, f"{key}: a full cover painted something at the right edge"
