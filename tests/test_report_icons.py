"""The document's marks: a closed set, drawn rather than typed.

The set is closed for the reason every bounded token in this series is: the
write gate can then refuse an unknown name while the caller is present to be
told, instead of a report rendering a blank where a mark was meant to be.

Drawn outlines rather than emoji because an emoji is a different typeface at a
different weight in every renderer — a mark that looks deliberate on a Mac and
like a bug on the dyno is worse than no mark.
"""

import hashlib
import pathlib
import re

from contextvault.services.report_blocks import Block
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_icons import ICON_NAMES, icon_path
from contextvault.services.report_render_pptx import build_report_pptx
from contextvault.services.report_types import SectionResult

# The eleven the mockup offers. Written out rather than derived from the module
# under test, which would make this assertion agree with itself.
_MOCKUP = {
    "doc",
    "db",
    "chart",
    "trend",
    "warn",
    "check",
    "clock",
    "flag",
    "scales",
    "shield",
    "bank",
}

_TSX = pathlib.Path(__file__).resolve().parents[1] / "frontend/src/pages/reports/documentIcons.tsx"


def test_the_set_is_the_mockups_eleven_and_a_way_to_say_no_mark() -> None:
    assert sorted(ICON_NAMES) == sorted(_MOCKUP | {"none"})


def test_a_name_resolves_to_a_path_a_renderer_can_draw() -> None:
    for name in _MOCKUP:
        path = icon_path(name)
        assert path is not None and path.strip() != "", name
        # Every glyph is drawn on the same grid as the editor's own, so a mark
        # is the same weight beside a row as it is on the page.
        assert re.match(r"^[MmLlHhVvCcSsQqTtAaZz0-9 .,-]+$", path), name


def test_no_mark_and_an_unknown_name_both_draw_nothing() -> None:
    """``none`` is a reader's choice and an unknown name is a stale payload. The
    renderer treats them alike; only the *write* gate distinguishes them."""
    assert icon_path("none") is None
    assert icon_path("nonsense") is None
    assert icon_path("") is None


def _tsx_paths() -> dict[str, str]:
    """The glyph table out of the TypeScript file, by reading it.

    Crossing a language boundary in a test is unusual and is the point: a
    comment saying "mirrors report_icons.py" does not keep two constants in
    sync, and the editor drawing a different mark from the page is exactly the
    kind of drift nobody notices until a reader points at it.
    """
    source = _TSX.read_text(encoding="utf-8")
    body = re.search(r"const PATHS: Record<string, string> = \{(.*?)\n\};", source, re.S)
    assert body is not None, "PATHS literal not found in documentIcons.tsx"
    return dict(re.findall(r'^\s*(\w+):\s*"([^"]*)",\s*$', body.group(1), re.M))


def test_the_editor_and_the_page_draw_the_same_marks() -> None:
    mirrored = _tsx_paths()

    assert set(mirrored) == _MOCKUP
    for name, path in mirrored.items():
        assert path == icon_path(name), name


# --- the deck ignores the field, and that is a decision ----------------------


def test_a_deck_built_from_marked_blocks_is_the_deck_built_from_plain_ones() -> None:
    """**python-pptx has no inline SVG.** Drawing a mark on a slide would mean
    rasterising a glyph per occurrence at whatever the theme's ink happens to be
    — a rendering path, a font-colour question and a cache, for a decoration.

    A report is not less itself without one, so the deck ignores the field. This
    is asserted rather than left to a comment: the marks are stored on the same
    block list both renderers read, so "the deck ignores it" is a claim about
    code that could stop being true without anyone noticing.
    """
    sections = [
        SectionResult(
            title="Дохід",
            source="database",
            sub_question="дохід",
            kind="data",
            result=QueryResult(columns=["місто", "сума"], rows=[("Київ", 120)]),
        )
    ]
    plain = [Block(kind="heading", section=0), Block(kind="callout", text="Висновок")]
    marked = [
        Block(kind="heading", section=0, icon="db"),
        Block(kind="callout", text="Висновок", icon="warn"),
    ]

    def deck(blocks: list[Block]) -> bytes:
        return build_report_pptx(title="Звіт", sections=sections, language="uk", blocks=blocks)

    # Digests, never a bare `==` on large binaries — the failure output is
    # otherwise two megabytes of nothing anyone can read.
    assert hashlib.sha256(deck(marked)).hexdigest() == hashlib.sha256(deck(plain)).hexdigest()
