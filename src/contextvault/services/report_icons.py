"""The marks a heading or a callout may wear, and the lines that draw them.

A closed set, like every bounded token in this series: ``validate_blocks``
refuses a name that is not here, so a stale or mistyped payload is a 422 while
the caller is present rather than a blank space on a filed page.

**Drawn, never typed.** An emoji is a different typeface at a different weight in
every renderer — Apple's, Noto's, and whatever the dyno's fontconfig settles on —
so a mark chosen for a report would look deliberate on one machine and like a
defect on another. These are paths, so WeasyPrint draws them in the ink the
template already chose.

**Document-only.** ``report_pptx`` ignores the field: python-pptx cannot place
inline SVG, and rasterising a glyph per slide to add a decoration is a cost with
no reader on the other end of it. That is recorded there too.

The same eleven are mirrored in ``frontend/src/pages/reports/documentIcons.tsx``
so the picker draws what the page will. They cannot drift:
``tests/test_report_icons.py`` reads that file and compares the strings.
"""

__all__ = ["ICON_NAMES", "NO_ICON", "icon_path"]

# What a reader picks to mean "no mark". Distinct from the field being absent
# only at the write gate — both draw nothing — but a reader who has *chosen* no
# mark and one who never opened the picker are answering different questions,
# and the payload should be able to say which.
NO_ICON = "none"

# Drawn on the same 0–16 grid as the editor's own row glyphs (`blockIcons.tsx`),
# at the same stroke weight, so a mark is the same object in the tree and on the
# page. Kept deliberately plain: these sit at body size beside a heading, and a
# glyph with interior detail turns into a smudge at 10 pt.
_PATHS: dict[str, str] = {
    "doc": "M4 2h6l2 2v10H4z M10 2v2h2",
    "db": "M3 4c0-1.1 2.2-2 5-2s5 .9 5 2-2.2 2-5 2-5-.9-5-2z M3 4v8c0 1.1 2.2 2 5 2s5-.9 5-2V4",
    "chart": "M3 13V7 M6.5 13V3 M10 13V9 M13.5 13V5",
    "trend": "M2 12l4-4 3 3 5-6 M14 5v3 M14 5h-3",
    "warn": "M8 2l6 11H2z M8 6v4 M8 11.5v.5",
    "check": "M3 8.5l3.5 3.5L13 5",
    "clock": "M8 2a6 6 0 100 12A6 6 0 008 2z M8 4.5V8l2.5 1.5",
    "flag": "M4 14V2 M4 3h8l-2 2.5L12 8H4",
    "scales": "M8 3v10 M4 13h8 M3 6h10 M3 6l-1.5 3.5h3z M13 6l1.5 3.5h-3z",
    "shield": "M8 2l5 2v4c0 3-2.2 5.2-5 6-2.8-.8-5-3-5-6V4z",
    "bank": "M2 6l6-3.5L14 6 M3 6v6 M6.5 6v6 M9.5 6v6 M13 6v6 M2 13h12",
}

ICON_NAMES = frozenset(_PATHS) | {NO_ICON}


def icon_path(name: str) -> str | None:
    """The lines for a mark, or ``None`` for "draw nothing".

    ``none`` and an unrecognised name answer alike on purpose. The renderer's
    job is to draw what it can and leave the rest of the document standing —
    read tolerance, the same rule an unknown *kind* follows — and distinguishing
    a reader's choice from a stale payload is the write gate's job, where there
    is somebody to tell.
    """
    return _PATHS.get(name)
