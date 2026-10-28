"""The shipped families, and the two ways this has silently gone wrong before.

A missing 700 file does not fail — WeasyPrint embeds a font *named*
``<Family>-Bold`` whose program is a synthesised emboldening of the regular
outlines, so an assertion on the embedded name passes while the page shows a
fake bold. And DejaVu is installed in the production image, so "some font was
embedded" is not evidence either: it is exactly what gets substituted.
"""

from pathlib import Path

import pytest

# fontTools ships no py.typed marker; the ignore is per-import rather than a
# project-wide ignore_missing_imports, which would also silence a genuinely
# missing dependency.
from fontTools.ttLib import TTFont  # type: ignore[import-untyped]

from contextvault.services.report_fonts import _FONT_FILES
from contextvault.services.report_style import MONO, SANS, SANS_ALT, SERIF, SERIF_ALT

FONT_DIR = Path(__file__).resolve().parents[1] / "src" / "contextvault" / "assets" / "fonts"

# Ukrainian needs these four beyond the Russian Cyrillic block, and the shipping
# default language is uk.
UKRAINIAN = "ґєії"


@pytest.mark.parametrize(
    "filename",
    ["Inter-Regular.ttf", "Inter-Bold.ttf", "SourceSerif4-Regular.ttf", "SourceSerif4-Bold.ttf"],
)
def test_the_font_file_ships(filename: str) -> None:
    assert (FONT_DIR / filename).is_file()


@pytest.mark.parametrize(
    "filename",
    ["Inter-Regular.ttf", "Inter-Bold.ttf", "SourceSerif4-Regular.ttf", "SourceSerif4-Bold.ttf"],
)
def test_the_font_covers_ukrainian_cyrillic(filename: str) -> None:
    cmap = TTFont(FONT_DIR / filename).getBestCmap()
    missing = [ch for ch in UKRAINIAN if ord(ch) not in cmap]
    assert not missing, f"{filename} is missing {missing}"


def test_a_licence_ships_with_the_fonts() -> None:
    assert list(FONT_DIR.glob("*OFL*")), "SIL OFL text must ship beside the fonts"


def test_bold_is_a_real_file_not_a_synthesised_face() -> None:
    """The regular and bold programs must differ. If a family shipped only a
    400 face, both lookups would resolve to the same path and the PDF would
    carry a fabricated bold that still asserts correctly by name."""
    for family in ("Report Sans Alt", "Report Serif Alt"):
        regular = _FONT_FILES.resolve(family, weight=400)
        bold = _FONT_FILES.resolve(family, weight=700)
        assert regular != bold, f"{family}: bold resolves to the regular file"


def test_sans_serif_mono_stay_on_dejavu() -> None:
    """The five templates name these three, so a shipped file behind any of them
    would change four templates' default typeface and restyle every stored
    report. The goldens cannot catch it: they capture ``build_report_html``,
    which never resolves a font file — this test is the only thing standing
    between an edit to ``_SHIPPED`` and a silent restyle of ~470 reports."""
    for family in (SANS, SERIF, MONO):
        for weight in (400, 700):
            assert "assets/fonts" not in _FONT_FILES.resolve(family, weight)


def test_alt_families_resolve_to_the_shipped_files() -> None:
    assert _FONT_FILES.resolve(SANS_ALT, 400) == str(FONT_DIR / "Inter-Regular.ttf")
    assert _FONT_FILES.resolve(SERIF_ALT, 700) == str(FONT_DIR / "SourceSerif4-Bold.ttf")
