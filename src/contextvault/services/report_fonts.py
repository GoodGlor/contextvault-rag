"""Which file backs a font family, and what to call it in a URL.

Split out of ``report_render`` when the design canvas needed the same faces over
HTTP that the printer reads off disk. The printer resolves a path and hands
WeasyPrint a ``file://`` source; a browser may not read ``file://``, so the
canvas asks a route for the same bytes. Both go through this module, so the
canvas cannot end up in a different typeface from the page it previews — which
is a failure that looks like a working report and no assertion about text can
see.

This module deliberately imports matplotlib but **not** WeasyPrint: the API layer
and ``report_html`` both reach it, and neither should pull a printer in.
"""

from pathlib import Path

from matplotlib import font_manager
from matplotlib.font_manager import FontProperties

from contextvault.services.report_style import MONO, SANS, SANS_ALT, SERIF, SERIF_ALT

__all__ = ["FONT_SLUGS", "font_file_for_slug", "font_path", "font_slug"]

_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

# A shipped family names its own files; a family with no files falls back to
# matplotlib's bundled DejaVu, which is what every template used before 5f. The
# fallback is what keeps the goldens byte-identical: nothing moves unless a
# pairing token asks for it.
#
# ⚠️ Adding SANS/SERIF/MONO here would not be caught by the HTML/PPTX goldens:
# they capture ``build_report_html``'s markup, which names a family but never
# resolves a file, so which file backs "Report Sans" is invisible to them. The
# only guard is tests/test_report_fonts.py::test_sans_serif_mono_stay_on_dejavu
# — keep it green, and do not treat a passing golden suite as proof this dict
# is safe.
_SHIPPED: dict[str, dict[int, str]] = {
    SANS_ALT: {400: "Inter-Regular.ttf", 700: "Inter-Bold.ttf"},
    SERIF_ALT: {400: "SourceSerif4-Regular.ttf", 700: "SourceSerif4-Bold.ttf"},
}
# Unchanged from before 5f — this is what keeps the goldens byte-identical.
_MPL_FALLBACK = {SANS: "DejaVu Sans", SERIF: "DejaVu Serif", MONO: "DejaVu Sans Mono"}


class _FontFileResolver:
    """A family and weight to an absolute file path.

    A shipped family (the ``*_ALT`` ones) resolves to its own file under
    ``assets/fonts``. Everything else — ``SANS``, ``SERIF``, ``MONO`` — falls
    back to ``font_manager.findfont`` against matplotlib's bundled DejaVu,
    exactly as every template resolved before this task. That fallback is what
    keeps the eleven byte-identity goldens green: a document that names no
    ``*_ALT`` family walks the same code path it always did.
    """

    def resolve(self, family: str, weight: int) -> str:
        shipped = _SHIPPED.get(family)
        if shipped is not None:
            return str(_FONT_DIR / shipped[weight])
        mpl_weight = "bold" if weight == 700 else "normal"
        return font_manager.findfont(
            FontProperties(family=_MPL_FALLBACK[family], weight=mpl_weight)
        )


_FONT_FILES = _FontFileResolver()

# A closed set, and the route takes a key into it rather than anything resembling
# a path. A traversal attempt is therefore a 404 by construction — there is no
# sanitising step to get wrong, because nothing from the request is ever joined
# to a directory.
FONT_SLUGS: dict[tuple[str, int], str] = {
    (SANS, 400): "sans-400",
    (SANS, 700): "sans-700",
    (SERIF, 400): "serif-400",
    (SERIF, 700): "serif-700",
    (MONO, 400): "mono-400",
    (MONO, 700): "mono-700",
    (SANS_ALT, 400): "inter-400",
    (SANS_ALT, 700): "inter-700",
    (SERIF_ALT, 400): "serif4-400",
    (SERIF_ALT, 700): "serif4-700",
}
_BY_SLUG: dict[str, tuple[str, int]] = {slug: pair for pair, slug in FONT_SLUGS.items()}


def font_slug(family: str, weight: int) -> str:
    """The URL name for a face. Raises for a family the canvas cannot serve."""
    return FONT_SLUGS[(family, weight)]


def font_path(family: str, weight: int) -> str:
    """The absolute path to the file backing a family at a weight."""
    return _FONT_FILES.resolve(family, weight)


def font_file_for_slug(slug: str) -> str | None:
    """The file a slug names, or ``None`` — never a path built from input."""
    pair = _BY_SLUG.get(slug)
    return None if pair is None else font_path(*pair)
