"""Every value path reaching the document is escaped — swept, not sampled.

``test_report_html.py`` already asserts escaping on the paths that were *added*
by the change that introduced them: a cell value, the report title, a column
label, a section title, a callout's words, four block overrides. Each was
written where its feature was written, which means the suite proves ``_text``
escapes **the paths somebody remembered**. It cannot answer the question this
module exists for: *does every value reaching the markup go through it?*

Two things make that question worth a module of its own.

**The canvas mounts this markup directly** (#156). In the printed path a missed
escape is wrong output; in the canvas it is script in the reader's session. Yet
``preview=True`` — the exact document the canvas mounts — was not asserted for
escaping anywhere before this module.

**Not every field is protected by escaping**, and the difference matters when
somebody edits the renderer later. ``align`` is protected by an allow-list,
``icon`` by a dict lookup, ``kind`` and ``variant`` by ``==`` comparison, and a
chart by being base64 in a data URI. Those are safe *by construction*, and the
construction is invisible — a future change interpolating one of them directly
would be silently exploitable. The last group of tests pins the construction.

The sweep asserts two things per field, and the second is the load-bearing one:
the raw payload is **absent**, and the escaped payload is **present**. Absence
alone passes when a value is silently dropped, which would hide the very path it
claims to cover.
"""

import datetime
import uuid
from typing import Any

import pytest

from contextvault.services.report_blocks import Block
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_html import (
    CoverBrand,
    ReportAssetRef,
    build_report_html,
)
from contextvault.services.report_types import SectionResult, SectionSourceRef

# Chosen so a leak is unambiguous in either context: the angle brackets break out
# of text, the leading quote breaks out of an attribute. A bare `<script>` would
# not catch an attribute-context miss.
HOSTILE = '"><img src=x onerror=alert(1)>'
ESCAPED = "&quot;&gt;&lt;img src=x onerror=alert(1)&gt;"

ASSET_ID = uuid.UUID(int=7)
REPORT_DATE = datetime.date(2026, 8, 10)


def _document(**overrides: Any) -> str:
    """A document with one hostile value in it, print path by default."""
    base: dict[str, Any] = {
        "title": "Звіт",
        "sections": [
            SectionResult(
                title="Розділ",
                source="database",
                sub_question="питання",
                kind="data",
                result=QueryResult(columns=["city"], rows=[("Київ",)]),
                column_labels={"city": "Місто"},
            )
        ],
        "language": "uk",
        "report_date": REPORT_DATE,
    }
    base.update(overrides)
    return build_report_html(**base)


def _data_section(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "title": "Розділ",
        "source": "database",
        "sub_question": "питання",
        "kind": "data",
        "result": QueryResult(columns=["city"], rows=[("Київ",)]),
        "column_labels": {"city": "Місто"},
    }
    return SectionResult(**{**base, **overrides})


def _text_section(**overrides: Any) -> SectionResult:
    base: dict[str, Any] = {
        "title": "Розділ",
        "source": "documents",
        "sub_question": "питання",
        "kind": "text",
        "prose": "Текст [1]",
        "sources": [SectionSourceRef(number=1, title="Звіт.pdf", source_id=uuid.uuid4())],
    }
    return SectionResult(**{**base, **overrides})


# Every value path that reaches the markup, with where it comes from. The origin
# is not decoration: a value the *reader* supplies and a value a *customer's
# database* supplies are both outside the app's control, and both land here.
VALUE_PATHS: list[tuple[str, dict[str, Any]]] = [
    # reader-supplied
    ("report title", {"title": HOSTILE}),
    ("section title", {"sections": [_data_section(title=HOSTILE)]}),
    # model-supplied
    ("section prose", {"sections": [_text_section(prose=HOSTILE)]}),
    ("column label", {"sections": [_data_section(column_labels={"city": HOSTILE})]}),
    # database-supplied
    (
        "table cell value",
        {"sections": [_data_section(result=QueryResult(columns=["c"], rows=[(HOSTILE,)]))]},
    ),
    (
        "raw column name",
        {
            "sections": [
                _data_section(
                    result=QueryResult(columns=[HOSTILE], rows=[("Київ",)]),
                    column_labels={},
                )
            ]
        },
    ),
    ("database error text", {"sections": [_data_section(kind="error", error=HOSTILE)]}),
    # upload-supplied
    (
        "citation source title",
        {
            "sections": [
                _text_section(
                    sources=[SectionSourceRef(number=1, title=HOSTILE, source_id=uuid.uuid4())]
                )
            ]
        },
    ),
    (
        "uploaded asset mime type",
        {
            "blocks": [Block(kind="image", asset_id=ASSET_ID)],
            "assets": {ASSET_ID: ReportAssetRef(mime=HOSTILE, data=b"\x89PNG")},
        },
    ),
    # An uploaded mime reaches a `data:` URI from *two* places, and covering one
    # is not covering the other: the image block, and the cover logo.
    (
        "cover logo mime type",
        {
            "blocks": [Block(kind="cover"), Block(kind="heading", section=0)],
            "brand": CoverBrand(logo_asset_id=ASSET_ID),
            "assets": {ASSET_ID: ReportAssetRef(mime=HOSTILE, data=b"\x89PNG")},
        },
    ),
    # design overrides, stored and replayed on every later render
    ("block text override", {"blocks": [Block(kind="heading", section=0, text=HOSTILE)]}),
    (
        "image block caption",
        {
            "blocks": [Block(kind="image", asset_id=ASSET_ID, caption=HOSTILE)],
            "assets": {ASSET_ID: ReportAssetRef(mime="image/png", data=b"\x89PNG")},
        },
    ),
]


@pytest.mark.parametrize("name,overrides", VALUE_PATHS, ids=[n for n, _ in VALUE_PATHS])
def test_a_hostile_value_reaches_the_printed_document_escaped(
    name: str, overrides: dict[str, Any]
) -> None:
    html = _document(**overrides)
    assert HOSTILE not in html, f"{name} reaches the printed markup unescaped"
    assert ESCAPED in html, f"{name} never reached the markup at all — the case proves nothing"


@pytest.mark.parametrize("name,overrides", VALUE_PATHS, ids=[n for n, _ in VALUE_PATHS])
def test_a_hostile_value_reaches_the_canvas_document_escaped(
    name: str, overrides: dict[str, Any]
) -> None:
    """The preview document is the one the workbench canvas mounts into a shadow
    root, so this is the path where a miss stops being wrong and starts being
    exploitable. It was asserted nowhere before this module."""
    html = _document(preview=True, **overrides)
    assert HOSTILE not in html, f"{name} reaches the canvas markup unescaped"
    assert ESCAPED in html, f"{name} never reached the markup at all — the case proves nothing"


# --- safe by construction, not by escaping ------------------------------------
#
# These fields never reach the markup as text. Nothing would *fail* today if one
# started to — which is exactly why they are pinned here.


# Each field is only read by certain block kinds, and a case that puts it on a
# kind that ignores it asserts nothing. `align` on a heading was exactly that:
# it survived deleting the allow-list it claimed to protect.
ENUM_CARRIERS: list[tuple[str, dict[str, Any]]] = [
    # `class="img-block {align}"` — an unescaped class attribute, where the
    # allow-list is the only thing standing between a design and the markup.
    (
        "block align",
        {
            "blocks": [Block(kind="image", asset_id=ASSET_ID, align=HOSTILE)],
            "assets": {ASSET_ID: ReportAssetRef(mime="image/png", data=b"\x89PNG")},
        },
    ),
    # Only ever `==`-compared, against "table" and "chart".
    ("block variant", {"blocks": [Block(kind="viz", section=0, variant=HOSTILE)]}),
    # A dict lookup in report_icons; an unknown name draws nothing.
    ("block icon", {"blocks": [Block(kind="heading", section=0, icon=HOSTILE)]}),
    # Dispatch only. An unrecognised kind draws nothing at all — read tolerance,
    # so a design written by a later version cannot make a report unrenderable.
    ("block kind", {"blocks": [Block(kind=HOSTILE, section=0)]}),
]


@pytest.mark.parametrize("name,overrides", ENUM_CARRIERS, ids=[n for n, _ in ENUM_CARRIERS])
def test_a_hostile_block_enum_never_reaches_the_markup(
    name: str, overrides: dict[str, Any]
) -> None:
    """These never reach the markup as text, so escaping is not what protects
    them — an allow-list, a dict lookup and `==` are. Nothing would fail today
    if one started being interpolated instead, which is why it is pinned."""
    html = _document(**overrides)
    assert HOSTILE not in html, f"{name} reaches the markup unescaped"
    assert ESCAPED not in html, f"{name} now reaches the markup as text"


def test_a_hostile_cover_variant_never_reaches_the_markup() -> None:
    html = _document(brand=CoverBrand(variant=HOSTILE))
    assert HOSTILE not in html
    assert ESCAPED not in html


def test_a_chart_is_embedded_as_base64_and_cannot_carry_markup() -> None:
    """A chart is the one thing in the document that *is* markup — an SVG. It is
    safe because it is base64 in a data URI rather than inlined, so its bytes
    cannot become elements no matter what the drawing code emitted. Inlining it
    would be the natural "improvement" to make one day; this is the tripwire."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    html = _document(sections=[_data_section(chart_svg=svg, chart_spec={"chart_type": "bar"})])
    assert "<script>alert(1)</script>" not in html
    assert "data:image/svg+xml;base64," in html


def test_a_chart_alt_text_is_escaped() -> None:
    """The chart's ``alt`` is the section title, and it sits in an attribute."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    html = _document(
        sections=[_data_section(title=HOSTILE, chart_svg=svg, chart_spec={"chart_type": "bar"})]
    )
    assert HOSTILE not in html
    assert ESCAPED in html
