"""PPTX assembly — the deck must carry the same formatted values as the PDF."""

import time
import uuid
import zipfile
from dataclasses import replace
from decimal import Decimal
from io import BytesIO

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.shapes.picture import Picture

from contextvault.services.report_blocks import Block
from contextvault.services.report_chart import render_chart
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_format import label
from contextvault.services.report_html import _MAX_STAT_COLUMNS as HTML_MAX_STAT_COLUMNS
from contextvault.services.report_html import ReportAssetRef
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_render_pptx import (
    _MAX_STAT_COLUMNS as PPTX_MAX_STAT_COLUMNS,
)
from contextvault.services.report_render_pptx import _with_fixed_timestamps, build_report_pptx
from contextvault.services.report_style import TEMPLATES
from contextvault.services.report_types import SectionResult, SectionSourceRef

RESULT = QueryResult(
    columns=["fiscal_year", "value"],
    rows=[(2024, Decimal("3.6498000E+11")), (2023, Decimal("3.5258300E+11"))],
)


def _data_section(result: QueryResult, chart_png: bytes | None = None) -> SectionResult:
    return SectionResult(
        title="Активи Apple",
        source="database",
        sub_question="активи",
        kind="data",
        result=result,
        chart_png=chart_png,
        # default_blocks decides whether a chart block exists from chart_svg, not
        # chart_png (reports.py always sets both from the same ChartImages); this
        # fixture stands in for that pairing rather than caring about SVG content.
        chart_svg=chart_png,
        generated_sql="SELECT 1",
        column_labels={"fiscal_year": "Рік", "value": "Сума"},
    )


def _texts(blob: bytes) -> list[str]:
    """Every string the deck actually renders, across shapes and table cells."""
    out: list[str] = []
    for slide in Presentation(BytesIO(blob)).slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    out.extend(cell.text for cell in row.cells)
    return out


def _titles(blob: bytes) -> list[str]:
    """The first text shape on each slide — the slide's title, where it has one."""
    out: list[str] = []
    for slide in Presentation(BytesIO(blob)).slides:
        texts = [s.text_frame.text for s in slide.shapes if s.has_text_frame]
        out.append(texts[0] if texts else "")
    return out


def _slide_count(blob: bytes) -> int:
    return len(Presentation(BytesIO(blob)).slides)


def _chart_png() -> bytes:
    """A real PNG. ``add_picture`` parses the bytes, so a placeholder will not do."""
    png = render_chart(
        RESULT,
        ChartSpec(chart_type="bar", x_column="fiscal_year", y_column="value"),
        language="uk",
    )
    assert png is not None
    return png


def _text_section() -> SectionResult:
    return SectionResult(
        title="Політика відпусток",
        source="documents",
        sub_question="відпустки",
        kind="text",
        prose="Співробітники накопичують відпустку щомісяця.",
    )


def test_deck_is_a_valid_pptx_with_a_title_slide() -> None:
    blob = build_report_pptx(title="Звіт по активах", sections=[_data_section(RESULT)])
    assert blob[:2] == b"PK"  # OOXML is a zip container
    assert "Звіт по активах" in _texts(blob)


def test_values_are_formatted_not_raw_decimals() -> None:
    texts = _texts(build_report_pptx(title="Звіт", sections=[_data_section(RESULT)], language="uk"))
    assert not any("E+" in t for t in texts), texts
    assert any("млрд" in t for t in texts), texts


def test_localised_column_headings_are_used() -> None:
    texts = _texts(build_report_pptx(title="Звіт", sections=[_data_section(RESULT)], language="uk"))
    assert "Рік" in texts and "Сума" in texts
    assert "fiscal_year" not in texts


def test_year_column_is_not_totalled() -> None:
    texts = build_report_pptx(title="Звіт", sections=[_data_section(RESULT)], language="uk")
    joined = " ".join(_texts(texts))
    # The fixture's `value` column is itself labelled "Сума", so the totalled
    # line reads "Сума Сума: …" — awkward, and exactly what a column with that
    # name produces. The year's absence is the assertion that matters.
    assert "Сума Сума" in joined
    assert "Сума Рік" not in joined


def test_text_and_error_sections_render() -> None:
    text_section = SectionResult(
        title="Політика",
        source="documents",
        sub_question="правила",
        kind="text",
        prose="Відпустка триває 24 дні [1].",
        sources=[SectionSourceRef(number=1, title="policy.pdf", source_id=uuid.uuid4())],
    )
    error_section = SectionResult(
        title="Зламана секція", source="database", sub_question="q", kind="error", error="timeout"
    )
    texts = _texts(build_report_pptx(title="Звіт", sections=[text_section, error_section]))
    assert any("24 дні" in t for t in texts)
    assert any("timeout" in t for t in texts)


def test_empty_result_does_not_break_the_deck() -> None:
    empty = QueryResult(columns=["value"], rows=[])
    blob = build_report_pptx(title="Звіт", sections=[_data_section(empty)])
    assert blob[:2] == b"PK"


def test_chart_moves_the_table_to_its_own_slide() -> None:
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
        b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    with_chart = build_report_pptx(title="Звіт", sections=[_data_section(RESULT, png)])
    without = build_report_pptx(title="Звіт", sections=[_data_section(RESULT)])

    def count(blob: bytes) -> int:
        return len(Presentation(BytesIO(blob)).slides)

    assert count(with_chart) == count(without) + 1


# --- the deck wears the template -------------------------------------------------


def _slide_grounds(blob: bytes) -> set[str]:
    """The solid fill behind every slide, as an uppercase hex string."""
    return {
        str(slide.background.fill.fore_color.rgb) for slide in Presentation(BytesIO(blob)).slides
    }


def _run_colours(blob: bytes) -> set[str]:
    out: set[str] = set()
    for slide in Presentation(BytesIO(blob)).slides:
        for shape in slide.shapes:
            if not shape.has_text_frame:
                continue
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    if run.font.color and run.font.color.type is not None:
                        out.add(str(run.font.color.rgb))
    return out


def test_the_deck_takes_its_ground_from_the_template() -> None:
    """A white deck under a dark template is the same defect as a white chart.

    A deck has no stylesheet, so nothing propagates: every slide has to be
    painted, and a slide added later comes back white unless the helper does it.
    """
    blob = build_report_pptx(
        title="Звіт", sections=[_data_section(RESULT)], style=TEMPLATES["prezydia"]
    )
    assert _slide_grounds(blob) == {"14161D"}


def test_the_light_template_leaves_the_deck_light() -> None:
    blob = build_report_pptx(
        title="Звіт", sections=[_data_section(RESULT)], style=TEMPLATES["kvarta"]
    )
    assert _slide_grounds(blob) == {"F7F8FA"}


def test_the_text_is_legible_against_the_ground_it_sits_on() -> None:
    """Painting the ground and leaving the type black is worse than doing neither."""
    blob = build_report_pptx(
        title="Звіт", sections=[_data_section(RESULT)], style=TEMPLATES["prezydia"]
    )
    assert _run_colours(blob) == {"F2F5FA"}


def test_omitting_the_style_builds_the_default_template() -> None:
    blob = build_report_pptx(title="Звіт", sections=[_data_section(RESULT)])
    assert _slide_grounds(blob) == {"F7F8FA"}


def test_the_same_deck_twice_is_byte_identical() -> None:
    """A rebuild must reproduce its artifact, not merely resemble it.

    python-pptx writes each zip entry through ``zipfile.writestr`` with a bare
    filename, so the entry's ``date_time`` comes from the wall clock. Two builds
    inside one second agree and two builds seconds apart do not, which is why a
    probe that runs fast enough reports success. ``time.sleep`` here is the point
    of the test, not incidental to it: the variable under test is elapsed time.
    """
    section = _data_section(RESULT)
    first = build_report_pptx(title="Звіт", sections=[section], language="uk")
    time.sleep(2)
    second = build_report_pptx(title="Звіт", sections=[section], language="uk")
    assert first == second


def test_pinning_the_timestamps_changes_no_part_content() -> None:
    """The fix must move entry headers only — never a byte a reader can perceive."""
    section = _data_section(RESULT)
    blob = build_report_pptx(title="Звіт", sections=[section], language="uk")
    archive = zipfile.ZipFile(BytesIO(blob))
    assert archive.testzip() is None  # the rewritten package is a valid zip
    assert "ppt/presentation.xml" in archive.namelist()
    assert {info.date_time for info in archive.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_stamping_rewrites_headers_and_nothing_else() -> None:
    """Header-only is a literal claim: no part is inflated and none re-deflated.

    A package that keeps its length, and every entry its CRC, its compressed size
    and its compression method, is one whose part bytes were left alone. Rebuilding
    the archive happened to produce the same bytes only because one zlib build
    compressed both times; this is the assertion that says it never had to.
    """
    raw = BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", "<p:presentation/>" + " " * 4000)
        archive.writestr("docProps/thumbnail.jpeg", bytes(range(256)) * 8)
    before = raw.getvalue()
    after = _with_fixed_timestamps(before)

    def parts(blob: bytes) -> dict[str, tuple[int, int, int]]:
        return {
            info.filename: (info.CRC, info.compress_size, info.compress_type)
            for info in zipfile.ZipFile(BytesIO(blob)).infolist()
        }

    assert len(after) == len(before)
    assert parts(after) == parts(before)
    assert {i.date_time for i in zipfile.ZipFile(BytesIO(after)).infolist()} == {
        (1980, 1, 1, 0, 0, 0)
    }


def test_stored_block_list_reorders_the_slides() -> None:
    """The list decides slide order, not the sections list."""
    first, second = _data_section(RESULT), _text_section()
    forward = build_report_pptx(title="T", sections=[first, second], language="uk")
    reversed_blocks = [
        Block(kind="cover"),
        Block(kind="heading", section=1),
        Block(kind="prose", section=1),
        Block(kind="heading", section=0),
        Block(kind="stats", section=0),
        Block(kind="viz", section=0, variant="table"),
    ]
    swapped = build_report_pptx(
        title="T", sections=[first, second], language="uk", blocks=reversed_blocks
    )
    assert _titles(forward)[1] == "Активи Apple"
    assert _titles(swapped)[1] == "Політика відпусток"


def test_omitting_the_stats_block_drops_the_caption() -> None:
    section = _data_section(RESULT)
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="viz", section=0, variant="table"),
    ]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert not any(text.startswith("Рядків:") for text in _texts(blob))


def test_a_pagebreak_splits_one_slide_into_two() -> None:
    section = _text_section()
    without = [Block(kind="heading", section=0), Block(kind="prose", section=0)]
    with_break = [
        Block(kind="heading", section=0),
        Block(kind="pagebreak"),
        Block(kind="prose", section=0),
    ]
    one = build_report_pptx(title="T", sections=[section], language="uk", blocks=without)
    two = build_report_pptx(title="T", sections=[section], language="uk", blocks=with_break)
    assert _slide_count(two) == _slide_count(one) + 1


def test_unknown_kind_and_dangling_section_are_skipped_in_silence() -> None:
    section = _text_section()
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="sparkline", section=0),  # a kind this renderer does not know
        Block(kind="prose", section=99),  # a section that is not there
        Block(kind="prose", section=0),
    ]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert _slide_count(blob) == 1
    assert "Співробітники накопичують відпустку щомісяця." in _texts(blob)


def test_a_table_variant_where_the_default_chose_a_chart() -> None:
    """The section has a chart available; the list asks for the table instead."""
    section = _data_section(RESULT, chart_png=_chart_png())
    charted = [
        Block(kind="heading", section=0),
        Block(kind="viz", section=0, variant="chart"),
        Block(kind="viz", section=0, variant="table"),
    ]
    tabular = [
        Block(kind="heading", section=0),
        Block(kind="viz", section=0, variant="table"),
    ]
    with_chart = build_report_pptx(title="T", sections=[section], language="uk", blocks=charted)
    without = build_report_pptx(title="T", sections=[section], language="uk", blocks=tabular)
    assert _slide_count(with_chart) == 2  # chart consumes its slide, table continues
    assert _slide_count(without) == 1  # no chart, so no continuation


# --- review round 1 -----------------------------------------------------------


def test_a_block_after_a_table_does_not_overlap_it() -> None:
    """Finding 1: a table never advanced the cursor, so a callout landed on it."""
    section = _data_section(RESULT)
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="viz", section=0, variant="table"),
        Block(kind="callout", text="Зверніть увагу"),
    ]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    slide = Presentation(BytesIO(blob)).slides[0]
    table_top = next(s.top for s in slide.shapes if getattr(s, "has_table", False))
    callout_top = next(s.top for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE)
    assert callout_top > table_top


def test_a_group_that_draws_nothing_produces_no_slide() -> None:
    """Finding 2: a stale heading index must degrade to silence, not a blank slide."""
    section = _text_section()
    blocks = [Block(kind="heading", section=9), Block(kind="prose", section=9)]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert _slide_count(blob) == 0


def test_an_unknown_kind_alone_produces_no_slide() -> None:
    """Finding 2, the sibling case: a kind this renderer does not know at all."""
    blob = build_report_pptx(
        title="T", sections=[], language="uk", blocks=[Block(kind="sparkline")]
    )
    assert _slide_count(blob) == 0


def test_the_untitled_slide_case_still_draws() -> None:
    """Finding 2's fix must not swallow the one deliberately untitled slide."""
    section = _text_section()
    blocks = [Block(kind="prose", section=0)]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert _slide_count(blob) == 1
    assert "Співробітники накопичують відпустку щомісяця." in _texts(blob)


def test_a_cover_mid_list_does_not_reopen_the_previous_section() -> None:
    """Finding 3: a cover must end the open section, not leave it bound."""
    section = _text_section()
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="prose", section=0),
        Block(kind="cover"),
        Block(kind="prose", section=0),
    ]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    # heading+prose, then the mid-list cover's own title slide, then the trailing
    # prose — untitled, since the cover ended the section it would otherwise reopen.
    assert _slide_count(blob) == 3
    assert _titles(blob)[2] == "Співробітники накопичують відпустку щомісяця."


# --- review round 2 -----------------------------------------------------------


def test_a_chart_block_with_no_image_produces_no_blank_slide() -> None:
    """A chart block needs an actual picture before it is worth a slide.

    A ``viz``/``chart`` block whose section resolves but carries no ``chart_png``
    (chart no longer available, or never rendered) is placed by ``_draw_group``
    itself rather than by ``_draw_block``, and it asks for no canvas when there
    is no picture — so it costs no slide instead of a blank one.
    """
    section = _data_section(RESULT)  # chart_png defaults to None
    blocks = [Block(kind="cover"), Block(kind="viz", section=0, variant="chart")]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert _slide_count(blob) == 1  # just the cover/title slide, no blank second slide


def test_a_callout_renders_with_no_bound_section() -> None:
    """The only kind carrying its own text is the only one that needs no section.

    That is what lets a callout survive a list whose section indexes have gone
    stale, so it needs no index guard and must not acquire one.
    """
    section = _text_section()
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="callout", text="Дані попередні."),
    ]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert "Дані попередні." in _texts(blob)


def test_a_callout_survives_a_dangling_section_index() -> None:
    section = _text_section()
    blocks = [Block(kind="callout", section=99, text="Дані попередні.")]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert "Дані попередні." in _texts(blob)


# --- final review: no slide may carry zero shapes -----------------------------


def _shape_counts(blob: bytes) -> list[int]:
    """How many shapes each slide actually carries. A zero here is a blank slide."""
    return [len(slide.shapes) for slide in Presentation(BytesIO(blob)).slides]


def test_a_chart_continuation_that_draws_nothing_makes_no_slide() -> None:
    """A group-level predicate cannot see the slide a chart's continuation opens.

    The chart fills slide one and the ``sources`` block that follows it opens the
    continuation; the section has no sources, so that continuation received a
    title-less, shape-less slide. Only creating the slide from the path that
    places a shape can close this.
    """
    section = _data_section(RESULT, chart_png=_chart_png())  # sources defaults to ()
    blocks = [
        Block(kind="viz", section=0, variant="chart"),
        Block(kind="sources", section=0),
    ]
    blob = build_report_pptx(title="T", sections=[section], language="uk", blocks=blocks)
    assert _shape_counts(blob) == [1]  # the picture, and no continuation behind it


def test_a_sources_block_with_no_sources_makes_no_slide() -> None:
    """``_section_of`` resolves, so the old predicate said yes; ``_draw_block`` drew nothing."""
    section = _data_section(RESULT)
    blob = build_report_pptx(
        title="T", sections=[section], language="uk", blocks=[Block(kind="sources", section=0)]
    )
    assert _shape_counts(blob) == []


def test_a_table_over_a_section_with_no_result_makes_no_slide() -> None:
    """The defensive branch: a ``data`` section carrying no result at all."""
    section = SectionResult(
        title="Активи Apple",
        source="database",
        sub_question="активи",
        kind="data",
        result=None,
        generated_sql="SELECT 1",
    )
    blob = build_report_pptx(
        title="T",
        sections=[section],
        language="uk",
        blocks=[Block(kind="viz", section=0, variant="table")],
    )
    assert _shape_counts(blob) == []


# --- 5d: hiding a block -------------------------------------------------------


def test_a_hidden_block_is_absent_from_the_deck() -> None:
    """5b made the deck render from the same block list; hide has to mean the
    same thing in both artifacts or the reader edits one and prints another."""
    section = _data_section(RESULT)
    blocks = [
        Block(kind="heading", section=0),
        Block(kind="prose", section=0, hidden=True),
        Block(kind="viz", section=0, variant="table"),
    ]
    visible = build_report_pptx(
        title="Звіт",
        sections=[section],
        language="uk",
        blocks=[b for b in blocks if not b.hidden],
    )
    with_hidden = build_report_pptx(title="Звіт", sections=[section], language="uk", blocks=blocks)
    assert _texts(with_hidden) == _texts(visible)


def test_hiding_the_only_block_of_a_slide_drops_the_slide() -> None:
    """The skip happens before grouping, so a hidden block never reserves space
    on a slide it does not draw on."""
    section = _data_section(RESULT)
    kept = build_report_pptx(
        title="Звіт",
        sections=[section],
        language="uk",
        blocks=[Block(kind="cover"), Block(kind="callout", text="Помітка")],
    )
    hidden = build_report_pptx(
        title="Звіт",
        sections=[section],
        language="uk",
        blocks=[Block(kind="cover"), Block(kind="callout", text="Помітка", hidden=True)],
    )
    assert _slide_count(hidden) == _slide_count(kept) - 1


# --- step 5e: the reader's own words, in the deck ------------------------------


def _prose_section() -> SectionResult:
    return SectionResult(
        title="Що показують дані",
        source="documents",
        sub_question="?",
        kind="text",
        prose="Дохід зріс на 12% [1].",
        sources=[SectionSourceRef(number=1, title="Звіт 2024", source_id=uuid.uuid4())],
    )


def test_the_deck_honours_a_heading_and_prose_override() -> None:
    texts = _texts(
        build_report_pptx(
            title="Звіт",
            sections=[_prose_section()],
            language="uk",
            blocks=[
                Block(kind="heading", section=0, text="Мій заголовок"),
                Block(kind="prose", section=0, text="Мій абзац."),
            ],
        )
    )
    assert "Мій заголовок" in texts
    assert "Мій абзац." in texts
    assert "Що показують дані" not in texts


def test_the_title_slide_takes_the_cover_override() -> None:
    texts = _texts(
        build_report_pptx(
            title="Згенерована назва",
            sections=[_prose_section()],
            language="uk",
            blocks=[Block(kind="cover", text="Квартальний звіт")],
        )
    )
    assert "Квартальний звіт" in texts
    assert "Згенерована назва" not in texts


def test_a_continuation_slide_repeats_the_overridden_section_title() -> None:
    """A continuation has no heading block of its own to read the override from,
    so it would otherwise print the generated title beside a first slide carrying
    the reader's — the same section, named two different ways in one deck."""
    texts = _texts(
        build_report_pptx(
            title="Звіт",
            sections=[_prose_section()],
            language="uk",
            blocks=[
                Block(kind="heading", section=0, text="Мій заголовок"),
                Block(kind="prose", section=0),
                Block(kind="pagebreak"),
                Block(kind="prose", section=0, text="Продовження."),
            ],
        )
    )
    assert texts.count("Мій заголовок") == 2
    assert "Що показують дані" not in texts


def test_the_deck_honours_a_sources_override() -> None:
    texts = _texts(
        build_report_pptx(
            title="Звіт",
            sections=[_prose_section()],
            language="uk",
            blocks=[
                Block(kind="heading", section=0),
                Block(kind="sources", section=0, text="[1] Внутрішній архів"),
            ],
        )
    )
    assert any("[1] Внутрішній архів" in text for text in texts)
    assert not any("Звіт 2024" in text for text in texts)


# --- 5f: image blocks and the agenda slide ------------------------------------


def _pictures(blob: bytes) -> list[Picture]:
    """Every picture shape in the deck, across all slides."""
    return [
        shape
        for slide in Presentation(BytesIO(blob)).slides
        for shape in slide.shapes
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE
    ]


def test_an_image_block_reaches_a_slide() -> None:
    asset_id = uuid.uuid4()
    blob = build_report_pptx(
        title="Звіт",
        sections=[_data_section(RESULT)],
        blocks=[Block(kind="heading", section=0), Block(kind="image", asset_id=asset_id)],
        assets={asset_id: ReportAssetRef(mime="image/png", data=_chart_png())},
    )
    assert _pictures(blob), "no picture shape reached the deck"


def test_an_image_block_keeps_its_aspect_ratio() -> None:
    """A picture stretched to the content width would distort a logo.

    ``add_picture`` scales the other dimension itself when only one is given, so
    this asserts the ratio survives rather than that a particular width was set.
    """
    asset_id = uuid.uuid4()
    blob = build_report_pptx(
        title="Звіт",
        sections=[_data_section(RESULT)],
        blocks=[Block(kind="heading", section=0), Block(kind="image", asset_id=asset_id)],
        assets={asset_id: ReportAssetRef(mime="image/png", data=_chart_png())},
    )
    picture = _pictures(blob)[0]
    source = Image.open(BytesIO(_chart_png()))
    expected = source.height / source.width
    assert abs(picture.height / picture.width - expected) < 0.02


def test_a_dangling_asset_leaves_the_deck_buildable_and_pictureless() -> None:
    """Assets are deleted explicitly, so a stored design can outlive its picture.

    The absence is asserted, not merely that nothing raised — the deck must not
    ship a slide holding a broken or empty frame.
    """
    # The image is the ONLY block, deliberately. ``canvas`` is lazy and shared
    # within a group, so an image sitting after a heading would reuse the slide
    # the heading already opened — and a guard that failed to decline would be
    # invisible. Alone, a block that draws nothing must produce no slide at all.
    blob = build_report_pptx(
        title="Звіт",
        sections=[_data_section(RESULT)],
        blocks=[Block(kind="image", asset_id=uuid.uuid4())],
        assets={},
    )
    assert not _pictures(blob)
    assert _slide_count(blob) == 0


def test_a_toc_becomes_an_agenda_slide_listing_the_sections() -> None:
    blob = build_report_pptx(
        title="Звіт",
        sections=[_data_section(RESULT), _text_section()],
        language="uk",
        blocks=[
            Block(kind="toc"),
            Block(kind="heading", section=0),
            Block(kind="heading", section=1),
        ],
    )
    texts = " ".join(_texts(blob))
    assert "Зміст" in texts
    assert _data_section(RESULT).title in texts
    assert _text_section().title in texts


def test_the_agenda_carries_no_page_numbers() -> None:
    """A deck has no pages, so a page column would be a lie.

    The document's toc gets its numbers from WeasyPrint's ``target-counter``;
    there is no such thing here, and inventing slide indexes would be a second
    numbering the reader never asked for.
    """
    blob = build_report_pptx(
        title="Звіт",
        sections=[_data_section(RESULT)],
        language="uk",
        blocks=[Block(kind="toc"), Block(kind="heading", section=0)],
    )
    agenda = next(t for t in _texts(blob) if "Зміст" in t or _data_section(RESULT).title in t)
    assert "target-counter" not in agenda


def test_a_hidden_heading_is_absent_from_the_agenda() -> None:
    blob = build_report_pptx(
        title="Звіт",
        sections=[_data_section(RESULT), _text_section()],
        language="uk",
        blocks=[
            Block(kind="toc"),
            Block(kind="heading", section=0, hidden=True),
            Block(kind="heading", section=1),
        ],
    )
    texts = " ".join(_texts(blob))
    assert _data_section(RESULT).title not in texts
    assert _text_section().title in texts


def test_a_deck_without_a_toc_gains_no_agenda_slide() -> None:
    """The identity guard: the deck goldens contain no toc block."""
    base = [Block(kind="heading", section=0)]
    without = _slide_count(
        build_report_pptx(
            title="Звіт", sections=[_data_section(RESULT)], language="uk", blocks=base
        )
    )
    with_toc = _slide_count(
        build_report_pptx(
            title="Звіт",
            sections=[_data_section(RESULT)],
            language="uk",
            blocks=[Block(kind="toc"), *base],
        )
    )
    assert with_toc == without + 1


# --- the over-aggregated section (#178) ---------------------------------------

_OVER_AGGREGATED = QueryResult(
    columns=["fiscal_year", "value"],
    rows=[
        (2017, Decimal("458469000000")),
        (2018, Decimal("531190000000")),
        (2019, Decimal("520674000000")),
    ],
)
"""One row per year, keys genuinely distinct, every figure two to three times
too large — the SQL summed quarterly, year-to-date and annual facts together."""


def _over_aggregated_section() -> SectionResult:
    section = _data_section(_OVER_AGGREGATED)
    return replace(section, overlap_warning="apple_financials", chart_png=None, chart_svg=None)


def test_an_over_aggregated_deck_states_its_row_count_and_no_total() -> None:
    """The document withholds Σ and x̄ for this shape and says why. The deck kept
    printing "Σ Сума: 1510,33 млрд" — the same wrong figure, in the artifact the
    nightly frozen schedule emits unwatched."""
    blob = build_report_pptx(title="Звіт", sections=[_over_aggregated_section()], language="uk")
    joined = " ".join(_texts(blob))
    assert "Рядків: 3" in joined
    # Spelled out, because `"Σ" not in joined` became unfailable the moment the
    # glyphs were replaced by words — and this is a #178 regression guard.
    assert "Сума Сума" not in joined
    assert "Середнє Сума" not in joined


def test_an_over_aggregated_deck_carries_the_caveat() -> None:
    """Losing the chart and keeping the total with nothing to explain it is
    strictly worse than the deck this branch started from. The slide must say the
    periods overlap, in the reader's language, beside the rows it is about."""
    blob = build_report_pptx(title="Звіт", sections=[_over_aggregated_section()], language="uk")
    assert any("перекриваються" in text for text in _texts(blob))
    english = build_report_pptx(
        title="Report", sections=[_over_aggregated_section()], language="en"
    )
    assert any("overlap" in text for text in _texts(english))


def test_a_sound_deck_carries_no_caveat() -> None:
    """The deck goldens capture whole packages; the caveat must be conditional."""
    blob = build_report_pptx(title="Звіт", sections=[_data_section(RESULT)], language="uk")
    assert not any("перекриваються" in text for text in _texts(blob))


def test_a_deck_withholds_the_total_when_the_keys_repeat() -> None:
    """#176's rule, which the document has applied since it was written and the
    deck silently did not: five rows for one year, one per period length."""
    repeated = QueryResult(
        columns=["fiscal_year", "value"],
        rows=[
            (2017, Decimal("78351000000")),
            (2017, Decimal("229234000000")),
            (2017, Decimal("196534000000")),
        ],
    )
    joined = " ".join(_texts(build_report_pptx(title="Звіт", sections=[_data_section(repeated)])))
    assert "Rows: 3" in joined
    # English here (no `language=`), and spelled out for the same reason as above.
    assert "Total Сума" not in joined
    assert "Average Сума" not in joined


def test_a_deck_shows_three_measure_columns_and_a_page_shows_four() -> None:
    """The two media cap the statistics differently, on purpose, and nothing
    measured the deck's number.

    A slide fits one line of stats and an A4 page fits a list, so
    ``_MAX_STAT_COLUMNS`` is 3 here and 4 in ``report_html`` — each recorded
    beside its own constant. Once #183 made the cap a *parameter* of the shared
    ``measure_summary``, a caller could pass the other medium's value and every
    test still passed: raising the deck's cap to 4 was invisible, because the
    HTML goldens only ever see the page's number. This pins the deck's, and
    asserts the two differ rather than asserting 3 twice — the failure mode the
    card names is unifying them, and a test that hard-codes one number in both
    places would be satisfied by exactly that.
    """
    five_measures = QueryResult(
        columns=["fiscal_year", "revenue", "profit", "expenses", "cash"],
        rows=[
            (2024, Decimal("100"), Decimal("10"), Decimal("40"), Decimal("7")),
            (2023, Decimal("200"), Decimal("20"), Decimal("50"), Decimal("8")),
        ],
    )
    joined = " ".join(
        _texts(build_report_pptx(title="Report", sections=[_data_section(five_measures)]))
    )
    # `fiscal_year` is a year, not a measure, so the four money columns are the
    # candidates and the cap decides how many of them are printed.
    assert joined.count(f"{label('stat_total', 'en')} ") == PPTX_MAX_STAT_COLUMNS
    assert PPTX_MAX_STAT_COLUMNS < HTML_MAX_STAT_COLUMNS


def test_a_merge_note_reaches_the_deck_too() -> None:
    """#204: the deck and the document are the same report. A disclosure the PDF
    makes and the PPTX does not is a disclosure the reader can miss by choosing
    the other format."""
    section = SectionResult(
        title="Дохід",
        source="database",
        sub_question="q",
        kind="data",
        result=QueryResult(columns=["a"], rows=[(1,)]),
        notes=["Немає даних із: Microsoft DB."],
    )
    deck = build_report_pptx(title="Звіт", sections=[section], language="uk")
    assert any("Microsoft DB" in text for text in _texts(deck))


def test_a_merged_chart_slide_names_its_databases() -> None:
    """#205: the deck has no stylesheet, so the caption becomes a text line. A
    disclosure the PDF makes and the PPTX does not is one the reader can miss by
    choosing the other format."""
    section = SectionResult(
        title="Дохід",
        source="database",
        sub_question="q",
        kind="data",
        result=QueryResult(columns=["a"], rows=[(1,)]),
        chart_png=(
            png := render_chart(
                QueryResult(columns=["y", "v"], rows=[(2024, 1.0), (2025, 2.0)]),
                ChartSpec(chart_type="bar", x_column="y", y_column="v", title="t"),
            )
        ),
        # `default_blocks` decides a chart block exists from chart_svg, as
        # `_data_section` above documents.
        chart_svg=png,
        databases=["Apple DB", "Microsoft DB"],
    )
    deck = build_report_pptx(title="Звіт", sections=[section], language="uk")
    assert any("Apple DB, Microsoft DB" in text for text in _texts(deck))


def test_a_single_database_chart_slide_gains_no_caption() -> None:
    section = SectionResult(
        title="Дохід",
        source="database",
        sub_question="q",
        kind="data",
        result=QueryResult(columns=["a"], rows=[(1,)]),
        chart_png=(
            png := render_chart(
                QueryResult(columns=["y", "v"], rows=[(2024, 1.0), (2025, 2.0)]),
                ChartSpec(chart_type="bar", x_column="y", y_column="v", title="t"),
            )
        ),
        chart_svg=png,
        databases=["Apple DB"],
    )
    deck = build_report_pptx(title="Звіт", sections=[section], language="uk")
    assert not any("Apple DB" in text for text in _texts(deck))
