"""Whether an uploaded file may become a report asset, decided on its bytes.

Raster only, and that is a rendering constraint rather than taste: python-pptx
places images with ``add_picture`` and supports no SVG, and no rasteriser is
installed. Accepting an SVG would render it in the document and leave a silent
gap in the deck — the same shape as the recorded WeasyPrint failure where an
unparseable image is dropped and a complete-looking, chartless PDF comes out.

Pure: no session, no request, no FastAPI. The endpoint decides who may upload;
this module decides what an upload *is*.
"""

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_ASSET_PIXELS = 4000
ACCEPTED_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})

# Pillow's format names, not MIME types. ``JPEG`` covers ``.jpg`` too.
_FORMAT_TO_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class ValidatedImage:
    """An upload that may be stored, with the facts the renderers need."""

    mime: str
    width: int
    height: int
    data: bytes


def validate_upload(data: bytes, *, filename: str) -> ValidatedImage:
    """The upload as a storable image, or ``ValueError`` saying why not.

    The message is shown to the reader, so it names the accepted formats rather
    than the rule that rejected them.
    """
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError("This file is larger than 2 MB. Please upload a smaller image.")

    # Detect SVG format manually since Pillow's SVG support may be unavailable.
    # SVG is detected by byte signature only; client-supplied filename is not trusted.
    if data.lstrip().startswith(b"<svg"):
        image_format: str | None = "SVG"
        width, height = 0, 0
    else:
        try:
            with Image.open(io.BytesIO(data)) as probe:
                # ``verify`` reads the whole stream and leaves the object unusable,
                # so the format and size are taken first.
                image_format = probe.format
                width, height = probe.size
                probe.verify()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ValueError(f"{filename} is not a readable image.") from exc

    mime = _FORMAT_TO_MIME.get(image_format or "")
    if mime is None:
        raise ValueError(
            f"{filename} is not a supported image. Please upload a PNG, JPEG or WebP — "
            "vector files such as SVG cannot be placed in the slide deck."
        )

    if width > MAX_ASSET_PIXELS or height > MAX_ASSET_PIXELS:
        raise ValueError(f"This image is larger than {MAX_ASSET_PIXELS} px on a side.")

    return ValidatedImage(mime=mime, width=width, height=height, data=data)
