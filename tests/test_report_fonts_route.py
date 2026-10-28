"""The canvas's font endpoint.

**Public on purpose.** A ``url()`` inside a stylesheet carries no bearer token,
and an authenticated font route would 401 every face and leave the canvas
rendering Cyrillic in whatever the browser falls back to — silently, which is the
failure mode ``_render_with_css``'s docstring already warns about and which no
assertion about text can see. What it serves is two OFL-licensed families and
matplotlib's DejaVu: no user data, and nothing to scope to a repository.
"""

import pathlib
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from contextvault.main import create_app
from contextvault.services.report_fonts import FONT_SLUGS, font_file_for_slug, font_slug
from contextvault.services.report_style import SANS, SANS_ALT

# Every TrueType/OpenType file starts with one of these.
_SFNT_MAGIC = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO")


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """No database and no session override: this route touches neither."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def test_a_shipped_family_resolves_to_its_own_file() -> None:
    path = font_file_for_slug(font_slug(SANS_ALT, 700))
    assert pathlib.Path(path or "").name == "Inter-Bold.ttf"


def test_the_default_family_still_falls_back_to_dejavu() -> None:
    assert "DejaVu" in (font_file_for_slug(font_slug(SANS, 400)) or "")


def test_an_unknown_slug_resolves_to_nothing_rather_than_a_path() -> None:
    """Nothing from the request is ever joined to a directory, so this is a miss
    in a dict rather than a sanitising step that could be got wrong."""
    assert font_file_for_slug("../../../../etc/passwd") is None
    assert font_file_for_slug("inter-900") is None
    assert font_file_for_slug("") is None


def test_every_slug_is_url_safe_and_unique() -> None:
    assert len(set(FONT_SLUGS.values())) == len(FONT_SLUGS)
    assert all(slug.replace("-", "").isalnum() for slug in FONT_SLUGS.values())


async def test_a_face_is_served_without_a_session(client: AsyncClient) -> None:
    """No Authorization header — the stylesheet has none to give."""
    response = await client.get(f"/api/report-fonts/{font_slug(SANS_ALT, 400)}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "font/ttf"
    assert response.content[:4] in _SFNT_MAGIC
    assert "immutable" in response.headers["cache-control"]


async def test_the_bytes_are_the_file_the_printer_reads(client: AsyncClient) -> None:
    """The canvas and the page must be the same typeface. Serving *a* font is not
    the property; serving *that* font is."""
    slug = font_slug(SANS_ALT, 700)
    response = await client.get(f"/api/report-fonts/{slug}")

    assert response.content == pathlib.Path(font_file_for_slug(slug) or "").read_bytes()


async def test_an_unknown_face_is_a_404(client: AsyncClient) -> None:
    assert (await client.get("/api/report-fonts/nonsense-400")).status_code == 404


async def test_a_real_filename_in_the_font_directory_is_still_a_404(client: AsyncClient) -> None:
    """The closed set is the whole guard, and this is what proves it is doing the
    work. A path-joining implementation would serve this file happily — it exists
    — while a dict lookup misses it. Asserting on ``..%2F..`` instead would prove
    nothing: Starlette decodes it and never matches the route, so that request
    would 404 whatever this handler did.
    """
    assert (await client.get("/api/report-fonts/Inter-Bold.ttf")).status_code == 404
