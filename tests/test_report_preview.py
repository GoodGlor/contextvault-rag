"""Preview mode: what the canvas gets that the printer never sees.

Two things are asserted here and nowhere else. First, that preview mode is
*additive* — strip what it adds and the document is character-for-character the
one WeasyPrint prints. That is the whole claim of spec decision 1: one renderer,
not two. Second, that none of it appears by default, which is what keeps the
eleven byte-identity goldens still.
"""

import re
import uuid
from typing import Any

from contextvault.services.report_blocks import Block
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_fonts import font_slug
from contextvault.services.report_html import build_report_html
from contextvault.services.report_style import TEMPLATES, resolve_style
from contextvault.services.report_types import SectionResult, SectionSourceRef

RESULT = QueryResult(columns=["city", "revenue"], rows=[("Київ", 120), ("Львів", 80)])


def _data_section(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "title": "Дохід за містами",
        "source": "database",
        "sub_question": "дохід",
        "kind": "data",
        "result": RESULT,
    }
    return SectionResult(**{**base, **overrides})


BLOCKS = [
    Block(kind="cover"),
    Block(kind="heading", section=0),
    Block(kind="prose", section=0),
    Block(kind="viz", section=0, variant="table"),
]


def _build(**kwargs: Any) -> str:
    return build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", blocks=BLOCKS, **kwargs
    )


# One document holding every kind, so a kind that missed the attribute is caught
# here rather than by a reader who cannot type into a paragraph.
EVERY_KIND = [
    Block(kind="cover"),
    Block(kind="heading", section=0),
    Block(kind="prose", section=0),
    Block(kind="sources", section=0),
    Block(kind="callout", text="Висновок"),
    Block(kind="viz", section=0, variant="table"),
    Block(kind="stats", section=0),
    Block(kind="toc"),
    Block(kind="image"),
    Block(kind="pagebreak"),
]
# The indexes of `TEXT_KINDS` above — the five a reader may rewrite, and no others.
EDITABLE_INDEXES = [0, 1, 2, 3, 4]


def _cited_section(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "prose": "Текст",
        "sources": [SectionSourceRef(number=1, title="Звіт 2024", source_id=uuid.uuid4())],
    }
    return _data_section(**{**base, **overrides})


def _every_kind(**kwargs: Any) -> str:
    return build_report_html(
        title="Звіт", sections=[_cited_section()], language="uk", blocks=EVERY_KIND, **kwargs
    )


def _editable(html: str) -> list[int]:
    return sorted(int(found) for found in re.findall(r'data-block-text="(\d+)"', html))


def _indexes(html: str) -> list[int]:
    return sorted(int(found) for found in re.findall(r'data-block-index="(\d+)"', html))


def test_the_default_document_carries_no_block_anchors() -> None:
    assert "data-block-index" not in _build()


def test_every_visible_block_is_addressable_by_its_list_index() -> None:
    assert _indexes(_build(preview=True)) == [0, 1, 2, 3]


def test_a_hidden_block_does_not_shift_its_neighbours_numbers() -> None:
    """The tree indexes ``design.blocks``; the canvas must mean the same thing by
    "block 2". A hidden block renders nothing and keeps its number."""
    blocks = [
        Block(kind="cover"),
        Block(kind="heading", section=0, hidden=True),
        Block(kind="callout", text="Висновок"),
    ]
    html = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", blocks=blocks, preview=True
    )
    assert _indexes(html) == [0, 2]


def _body(html: str) -> str:
    found = re.search(r"<body[^>]*>(.*)</body>", html, re.S)
    assert found is not None
    return found.group(1)


def test_preview_is_additive_the_blocks_markup_is_identical() -> None:
    """Spec decision 1, asserted rather than asserted about.

    Preview adds the anchors, the editable marks, the sheet wrapper with its two
    strips, and the overlay stylesheet. Take those away and what is left is the
    printed document character for character — which is what "one renderer, not
    two" has to mean if it is to mean anything.

    **6c added one inline element and this test is where that stays honest.** The
    ``sources`` label shares its paragraph with the editable run, so the run needs
    a ``<span>`` of its own; it is unwrapped here before the attributes come off.
    The rule preview mode still obeys is the one whose reason matters — never a
    block-level element, never a wrapper around a block — because a ``<div>``
    inside ``columns: 2`` breaks ``column-span: all`` on the heading it nests.
    """
    canvas = _body(_every_kind(preview=True))
    # No running header: this document opens on a cover (see the test below).
    assert canvas.startswith('<div class="cv-sheet">')
    assert canvas.endswith("</footer></div>")

    unwrapped = re.sub(
        r'^<div class="cv-sheet">(<header class="cv-head">.*?</header>)?', "", canvas
    )
    unwrapped = re.sub(r'<footer class="cv-foot">.*?</footer></div>$', "", unwrapped)
    unwrapped = re.sub(
        r'<span data-block-text="\d+" contenteditable="true">(.*?)</span>', r"\1", unwrapped
    )
    stripped = re.sub(r' data-block-(index|text)="\d+"| contenteditable="true"', "", unwrapped)
    assert stripped == _body(_every_kind())


def test_the_canvas_stylesheet_only_appends_to_the_printed_one() -> None:
    """The overlay is appended, never interleaved: everything WeasyPrint is given
    is still there, in the same order, saying the same thing."""
    printed = _build().split("<style>")[1].split("</style>")[0]
    canvas = _build(preview=True).split("<style>")[1].split("</style>")[0]
    assert canvas.startswith(printed)


def test_every_block_kind_the_editor_can_show_is_addressable() -> None:
    """One document holding every kind, because the attribute is threaded through
    a dozen separate return sites and a kind that missed one is a block the reader
    can see and cannot click."""
    blocks = [
        Block(kind="cover"),
        Block(kind="toc"),
        Block(kind="heading", section=0),
        Block(kind="prose", section=0),
        Block(kind="viz", section=0, variant="table"),
        Block(kind="stats", section=0),
        Block(kind="sources", section=0),
        Block(kind="callout", text="Висновок"),
        Block(kind="image"),
        Block(kind="pagebreak"),
    ]
    section = _data_section(prose="Текст", sources=[])
    html = build_report_html(
        title="Звіт", sections=[section], language="uk", blocks=blocks, preview=True
    )
    # `sources` renders nothing without citations or an override — every other
    # kind must be addressable.
    assert _indexes(html) == [0, 1, 2, 3, 4, 5, 7, 8, 9]


# --- the sheet: page-box emulation -------------------------------------------


def test_the_sheet_reproduces_the_page_box_from_the_same_tokens() -> None:
    style = resolve_style("prezydiia")
    vertical, horizontal = style.page_margin_mm
    html = _build(style=style, preview=True)

    assert '<div class="cv-sheet">' in html
    assert f"padding: {vertical}mm {horizontal}mm" in html
    assert f"background: {style.paper}" in html
    assert f'font-family: "{style.body_family}"' in html


def test_the_default_document_has_no_sheet_and_no_overlay() -> None:
    html = _build()
    assert "cv-sheet" not in html
    assert "data-block-index" not in html


def test_the_sheet_scales_from_a_custom_property_defaulting_to_life_size() -> None:
    """The canvas scales the sheet by setting ``--cv-zoom`` on the host,
    which inherits across the shadow boundary; the sheet reads it here.

    ``zoom`` and not ``transform``: a transform paints a scaled picture of an
    unchanged layout box, so at 125 % the overflowing half of the sheet cannot be
    scrolled to. ``zoom`` resizes the box, which is what ``.cv-host``'s
    ``overflow-x: auto`` needs to see.

    **The fallback is what makes this inert.** A document rendered by anything that
    does not set the property — every caller but the canvas — is life-size, so this
    rule cannot change what any existing reader sees.
    """
    html = _build(preview=True)
    assert "zoom: var(--cv-zoom, 1)" in html


def test_the_printed_document_is_never_scaled() -> None:
    """The property is the canvas's, and the page box is millimetres. A ``zoom``
    reaching WeasyPrint would scale a document whose size is the whole point."""
    assert "--cv-zoom" not in _build()


def test_every_template_can_draw_a_sheet() -> None:
    """The overlay reads five templates' tokens; a KeyError in one of them is a
    blank canvas for that template alone, which no single-template test sees."""
    for key in TEMPLATES:
        assert "cv-sheet" in _build(style=resolve_style(key), preview=True)


def test_contents_page_numbers_are_a_dash_in_the_canvas_and_a_counter_in_print() -> None:
    """Spec decision 3. ``target-counter`` is WeasyPrint's; a browser drops the
    whole declaration as invalid and would show no number at all, which reads as
    a broken contents list rather than as an unpaginated one."""
    blocks = [Block(kind="toc"), Block(kind="heading", section=0)]
    printed = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", blocks=blocks
    )
    canvas = build_report_html(
        title="Звіт", sections=[_data_section()], language="uk", blocks=blocks, preview=True
    )

    assert "target-counter" in printed and '"—"' not in printed
    assert "target-counter" not in canvas and '"—"' in canvas


def test_the_canvas_shows_no_page_number_it_cannot_know() -> None:
    """The canvas does not paginate, so the footer carries the word and a dash.
    An invented number would be a lie in a filed document's editor."""
    html = _build(preview=True)
    assert "cv-foot" in html
    assert "counter(page)" not in html.split("<body")[1]


def test_the_running_header_names_the_document_without_executing_it() -> None:
    html = build_report_html(
        title="</header><script>alert(1)</script>",
        sections=[_data_section()],
        language="uk",
        blocks=BLOCKS,
        preview=True,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# --- the faces, over HTTP -----------------------------------------------------


def test_the_canvas_declares_both_weights_of_every_family_it_names() -> None:
    """Weight 700 is declared for the reason ``_font_css`` records: a missing
    bold is *synthesised*, so the page shows a fake bold while every assertion
    about text passes."""
    style = resolve_style("chasopys")
    html = _build(style=style, preview=True)
    families = {style.body_family, style.display_family, style.numeral_family}

    assert html.count("@font-face") == len(families) * 2
    for family in families:
        for weight in (400, 700):
            assert f'src: url("/api/report-fonts/{font_slug(family, weight)}")' in html


def test_every_template_names_only_faces_the_route_can_serve() -> None:
    """A family missing from ``FONT_SLUGS`` raises here rather than shipping a
    canvas whose fonts 404 one template at a time."""
    for key in TEMPLATES:
        assert "@font-face" in _build(style=resolve_style(key), preview=True)


def test_the_canvas_never_asks_a_browser_for_a_file_url() -> None:
    assert "file://" not in _build(preview=True)


def test_the_printed_document_still_carries_no_font_rules() -> None:
    """They belong to ``report_render``, which hands WeasyPrint its own
    stylesheet. That is what makes this addition unable to move a golden."""
    assert "@font-face" not in _build()


def test_the_canvas_does_not_repeat_the_title_above_its_own_cover() -> None:
    """``@page :first { @top-left { content: none } }`` in the printed document,
    reproduced on the canvas's single sheet. Without it the title sits twice near
    the top — once small in the running header, once large on the cover — which
    is the very thing that print rule exists to prevent.

    Found by looking at the canvas and the PDF side by side. No assertion had
    anything to say about it until this one.
    """
    # On the element, never on the string: `.cv-head` is also a selector in the
    # overlay stylesheet, so `"cv-head" not in html` could never be true and the
    # assertion would be unfailable.
    with_cover = _build(preview=True)
    assert "<header" not in with_cover

    without_cover = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0)],
        preview=True,
    )
    assert '<header class="cv-head"><span>Звіт</span></header>' in without_cover


def test_a_hidden_cover_puts_the_running_header_back() -> None:
    """Hide and delete render the same document, so a hidden cover is a document
    that does not open on one — and it needs its header."""
    html = build_report_html(
        title="Звіт",
        sections=[_data_section()],
        language="uk",
        blocks=[Block(kind="cover", hidden=True), Block(kind="heading", section=0)],
        preview=True,
    )
    assert '<header class="cv-head">' in html


# --- editing on the sheet (6c) ------------------------------------------------


def test_the_printed_document_is_not_editable_anywhere() -> None:
    """The attribute is preview's, like every other thing this card adds. A
    printed page carrying ``contenteditable`` is a golden away from moving."""
    printed = _every_kind()
    assert "data-block-text" not in printed
    assert "contenteditable" not in printed


def test_exactly_the_kinds_the_write_gate_permits_are_editable_on_the_sheet() -> None:
    """``TEXT_KINDS`` and nothing else. This set decides what the sheet *offers*;
    ``validate_blocks`` is what makes the restriction true, and is untouched by
    this card. Offering more than it permits would be a 422 the reader cannot
    see the cause of — a paragraph that accepts typing and loses it."""
    assert _editable(_every_kind(preview=True)) == EDITABLE_INDEXES


def test_the_editable_run_is_the_text_and_never_the_whole_block() -> None:
    """A cover is a logo, an eyebrow, a heading, a rule and two signature lines;
    only the heading is the reader's. Reading ``textContent`` off the block would
    put the eyebrow and the date into the override the first time anyone typed."""
    html = _every_kind(preview=True)

    assert '<h1 data-block-text="0" contenteditable="true">Звіт</h1>' in html
    # The cover's own anchor stays on the block, so clicking the logo still
    # selects the cover; only the words are editable.
    assert 'data-block-index="0"' in html.split("<h1")[0].split('<div class="cover"')[1]


def test_a_heading_and_a_paragraph_are_their_own_editable_run() -> None:
    """Both are a single element whose whole content is the override, so they
    take the attribute directly — no wrapper, and nothing to strip but attributes."""
    html = _every_kind(preview=True)

    assert 'data-block-index="1"' in html and 'data-block-text="1"' in html
    assert re.search(r'<h2[^>]* data-block-text="1" contenteditable="true">', html) is not None
    assert re.search(r'<p[^>]* data-block-text="2" contenteditable="true">Текст</p>', html)


def test_the_sources_label_is_furniture_and_stays_outside_the_edit() -> None:
    """``Джерела / Sources:`` is the block's furniture, not its content — a reader
    editing the list is not asking for the word to disappear. It is also what
    forces the one inline element preview mode adds: the label precedes the
    editable span, so ``textContent`` of the run is the list alone."""
    html = _every_kind(preview=True)
    found = re.search(r'<p class="sources"[^>]*>(.*?)</p>', html)
    assert found is not None

    assert found.group(1).startswith("Джерела / Sources: <span data-block-text=")
    assert found.group(1).endswith("</span>")
    # The label is outside the span, so the override never swallows it.
    assert "Джерела" not in found.group(1).split(">", 2)[2]


def test_a_callout_is_editable_in_its_paragraph_not_its_box() -> None:
    """The box carries the block's own styling; the paragraph carries the words."""
    html = _every_kind(preview=True)
    assert '<div class="callout" data-block-index="4">' in html
    assert '<p data-block-text="4" contenteditable="true">Висновок</p>' in html


def test_no_computed_block_can_be_typed_into() -> None:
    """The document may not disagree with the database. A table, a figure, the
    numbers, the contents list and an image all print values derived from the
    stored result, and none of them opens to a caret."""
    # In the *body*, never the whole document: `[contenteditable="true"]` is also
    # a selector in the overlay stylesheet, so counting the string across the file
    # would count the CSS and quietly answer the wrong question — the same trap
    # `.cv-head` set for the running-header test.
    html = _body(_every_kind(preview=True))
    # Counted rather than spot-checked: an element list would have to be kept in
    # step with the markup, and the one it forgot is the one that leaks.
    assert html.count('contenteditable="true"') == len(EDITABLE_INDEXES)
    for computed in ("<table", '<nav class="toc"', '<div class="pagebreak"'):
        assert "contenteditable" not in html.split(computed)[1].split(">")[0]


def test_the_edit_mark_appears_on_reach_and_not_at_rest() -> None:
    """**Changed by #167, and the old assertion was the defect.**

    Until then the dashed frame was on ``[contenteditable="true"]`` itself, so
    every editable block wore one permanently and the document never looked like
    the document — the cover's box being the largest object on the sheet. The
    affordance survives; it stops being furniture.

    ``outline`` and never ``border``, for 6b's reason: a border is layout, and
    would move the very thing the canvas exists to show you — here by a pixel on
    every editable paragraph at once.
    """
    canvas = _every_kind(preview=True)

    # At rest: a cursor, and nothing that draws.
    at_rest = canvas.split('[contenteditable="true"] {')[1].split("}")[0]
    assert "cursor: text" in at_rest
    assert "outline:" not in at_rest and "border:" not in at_rest

    hovered = canvas.split('[contenteditable="true"]:hover {')[1].split("}")[0]
    assert "outline: 1px dashed" in hovered
    focused = canvas.split('[contenteditable="true"]:focus {')[1].split("}")[0]
    assert "outline: 2px solid" in focused
    # The margin rule is a shadow, not a border, for the same reason.
    # `"border:"`, the declaration — not the word. This rule's own comment
    # explains why a border is wrong, so a bare substring check fails on the
    # explanation rather than on the code: the `.cv-head` trap, mirrored.
    assert "box-shadow:" in focused and "border:" not in focused


def test_the_printed_stylesheet_knows_nothing_about_editing() -> None:
    assert "contenteditable" not in _every_kind()


def test_a_customers_own_text_cannot_execute_in_the_editor() -> None:
    """``_text`` has escaped since step 2, but what it guarded was a *printer*.

    6b changed the stakes without changing the code: the canvas writes this
    document into the SPA's own DOM with ``innerHTML``, so an unescaped angle
    bracket in an LLM's prose or a customer's database cell is script execution
    in the application, not a stray glyph on a page. 6c adds a path where the
    reader's own text goes in too. Nothing asserted that until here.
    """
    hostile = '<script>alert(1)</script><img src=x onerror="alert(2)">'
    html = build_report_html(
        title=hostile,
        sections=[_cited_section(title=hostile, prose=hostile)],
        language="uk",
        blocks=EVERY_KIND,
        preview=True,
    )

    # The raw string, never anywhere — not the substring `onerror=`, which appears
    # perfectly safely inside an escaped `<title>` as inert text and would make
    # this assertion about the wrong thing.
    assert hostile not in html
    assert "<script" not in html
    assert 'onerror="' not in html  # an attribute needs a real quote; `&quot;` is text
    assert html.count("&lt;script&gt;") >= 3  # the cover, the heading, the toc entry


# --- the document's marks (6d) -----------------------------------------------


def _marked(**kwargs: Any) -> str:
    blocks = [
        Block(kind="heading", section=0, icon="warn"),
        Block(kind="callout", text="Висновок", icon="check"),
    ]
    return build_report_html(
        title="Звіт", sections=[_cited_section()], language="uk", blocks=blocks, **kwargs
    )


def test_a_marked_heading_draws_its_glyph_before_the_words() -> None:
    html = _marked()
    assert re.search(r'<h2[^>]*><svg class="cv-icon"[^>]*>.*?</svg>Дохід за містами</h2>', html)


def test_a_marked_callout_puts_the_glyph_outside_the_words() -> None:
    """Inside the box, outside the paragraph — which is what makes the mark
    survive a reader selecting everything in the callout and retyping it."""
    html = _marked()
    assert '<div class="callout"><svg class="cv-icon"' in html
    assert "</svg><p>Висновок</p></div>" in html


def test_the_mark_is_not_part_of_what_a_reader_can_type_over() -> None:
    """6c put a caret in the heading. `contenteditable="false"` is what the
    attribute exists for: the browser treats the glyph as one atomic thing
    rather than as characters to delete. Preview only — it says nothing to a
    printer, and a stored report has no caret in it."""
    assert 'class="cv-icon" contenteditable="false"' in _marked(preview=True)
    assert "contenteditable" not in _marked()


def test_the_glyph_is_painted_by_attributes_rather_than_by_the_stylesheet() -> None:
    """**Found by looking at the page, not by an assertion — and this is the
    assertion that keeps it fixed.**

    WeasyPrint does not apply the HTML cascade to an inline SVG's painting. With
    `fill`/`stroke` in the stylesheet every glyph printed *filled*: a solid black
    disc where a clock face should be, a black rectangle for a document. Nothing
    raised, the markup was exactly as intended, and every test here passed.

    SVG presentation attributes are honoured by both renderers, so the paint
    lives on the element. This test exists to stop it moving back into CSS.
    """
    mark = re.search(r"<svg class=\"cv-icon\"[^>]*>", _marked())
    assert mark is not None
    assert 'fill="none"' in mark.group(0)
    assert 'stroke="currentColor"' in mark.group(0)
    assert 'stroke-width="1.3"' in mark.group(0)
    # And the rule that remains carries geometry only — a `fill` or `stroke`
    # declaration there is the defect coming back.
    rule = _marked().split(".cv-icon {")[1].split("}")[0]
    assert "fill" not in rule and "stroke" not in rule


def test_an_unmarked_document_carries_no_icon_rule_at_all() -> None:
    """The single hazard this card was warned about. The goldens capture the
    whole `<style>`, so an unconditional rule moves all eleven — and a golden
    that moves for a feature no golden exercises is a silent restyle of every
    stored report, not a passing test to regenerate."""
    plain = _every_kind()
    assert "cv-icon" not in plain


def test_the_icon_rule_arrives_with_the_first_marked_block() -> None:
    assert ".cv-icon {" in _marked()


def test_a_hidden_marked_block_does_not_bring_the_rule_with_it() -> None:
    """`_conditional_css` keys off *visible* blocks, like every other rule it
    gates — a hidden block renders nothing and needs no styling."""
    html = build_report_html(
        title="Звіт",
        sections=[_cited_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0, icon="warn", hidden=True)],
    )
    assert "cv-icon" not in html


def test_choosing_no_mark_draws_nothing_and_asks_for_no_rule() -> None:
    html = build_report_html(
        title="Звіт",
        sections=[_cited_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0, icon="none")],
    )
    assert "cv-icon" not in html


def test_a_mark_this_build_does_not_know_leaves_the_heading_standing() -> None:
    """Read tolerance, the rule an unknown *kind* already follows: a design
    written by a later version must never make an existing report unrenderable."""
    html = build_report_html(
        title="Звіт",
        sections=[_cited_section()],
        language="uk",
        blocks=[Block(kind="heading", section=0, icon="hologram")],
    )
    assert "cv-icon" not in html
    assert "Дохід за містами" in html
