"""What may be uploaded, decided on the bytes rather than on the client's claim.

A declared content-type is the client's assertion; ``Pillow.verify()`` is the
only thing that knows whether the bytes are the image they say they are.
"""

import io

import pytest
from PIL import Image

from contextvault.services.report_assets import (
    ACCEPTED_MIMES,
    MAX_ASSET_BYTES,
    validate_upload,
)


def _png(width: int = 40, height: int = 20) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_png_is_accepted_and_measured() -> None:
    result = validate_upload(_png(320, 64), filename="logo.png")
    assert result.mime == "image/png"
    assert (result.width, result.height) == (320, 64)


def test_svg_is_refused_with_a_reason() -> None:
    """python-pptx takes raster only and no rasteriser is installed, so an SVG
    would render in the document and leave a gap in the deck. Refusing at the
    door tells the reader now instead of at export."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    with pytest.raises(ValueError, match="PNG|JPEG|WebP"):
        validate_upload(svg, filename="logo.svg")


def test_bytes_that_are_not_an_image_are_refused() -> None:
    """The filename says png; the bytes do not. Pillow is what settles it."""
    with pytest.raises(ValueError, match="not a readable image"):
        validate_upload(b"not an image at all", filename="logo.png")


def test_an_oversized_file_is_refused() -> None:
    with pytest.raises(ValueError, match="2 MB"):
        validate_upload(b"\x89PNG" + b"\x00" * MAX_ASSET_BYTES, filename="big.png")


def test_an_oversized_canvas_is_refused() -> None:
    with pytest.raises(ValueError, match="4000"):
        validate_upload(_png(4200, 10), filename="wide.png")


def test_the_accepted_set_is_exactly_three_raster_formats() -> None:
    assert frozenset({"image/png", "image/jpeg", "image/webp"}) == ACCEPTED_MIMES
