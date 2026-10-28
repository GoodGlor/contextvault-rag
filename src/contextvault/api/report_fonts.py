"""The design canvas's font faces, over HTTP.

The printed document names a family and WeasyPrint reads the file off disk. A
browser cannot: ``file://`` is not fetchable from a page, and fontconfig cannot
see a font packaged inside a Python wheel either. So the canvas's ``@font-face``
rules point here, at the same files ``report_render`` prints from.
"""

import pathlib

from fastapi import APIRouter, HTTPException, Response, status

from contextvault.services.report_fonts import font_file_for_slug

router = APIRouter(tags=["report-fonts"])

# One year, immutable: the bytes behind a slug change only when the deployed code
# does, and the canvas asks for up to six faces on every mount.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.get("/report-fonts/{slug}")
async def get_report_font(slug: str) -> Response:
    """One font face. **Deliberately unauthenticated.**

    A ``url()`` inside a stylesheet carries no bearer token, so an authenticated
    route would 401 every face and the canvas would silently render in whatever
    the browser falls back to — a report in the wrong typeface, which is exactly
    the failure ``_render_with_css`` warns about and which no assertion about
    text can see. What is served is two SIL OFL families and matplotlib's DejaVu:
    no user data, and nothing to scope to a repository.

    ``slug`` is a key into a closed dict, never a path. A traversal attempt
    misses the dict and is a 404 — there is no sanitising step here to get wrong.
    """
    path = font_file_for_slug(slug)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown font.")
    return Response(
        content=pathlib.Path(path).read_bytes(),
        media_type="font/ttf",
        headers={"Cache-Control": _CACHE_CONTROL},
    )
