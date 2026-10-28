"""Image OCR via the repository's own configured vision model.

The local RapidOCR engine could not read Cyrillic (its dictionary is Chinese+English
only), so scanned Ukrainian/Russian documents ingested as gibberish. Instead of a
second local engine, images are transcribed by the *same* multimodal LLM a repository
already answers with — Gemini, GPT-4o, Claude, and OpenRouter models all read mixed
scripts well — using that provider's global key (design: global provider keys).

This module mirrors :mod:`contextvault.llm.models`: standalone per-provider async
functions dispatched by name, with every provider/network failure wrapped in
:class:`OCRError` so the ingestion layer records one clean failure. Any input image
(including iPhone HEIC, which the vision APIs do not accept) is normalized to JPEG
first, so a single code path handles every uploaded format.
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING

from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from openai import AsyncOpenAI
from pillow_heif import register_heif_opener

from contextvault.core.config import get_settings

if TYPE_CHECKING:
    from google import genai

# Teach Pillow to decode HEIC/HEIF (iPhone photos) so ``_to_jpeg`` can normalize them.
register_heif_opener()

__all__ = ["OCRError", "transcribe_image"]

# A transcription cap generous enough for a dense page of text; OCR output is longer
# than a chat answer, so it does not share ``llm_max_tokens`` (tuned for answers).
_OCR_MAX_TOKENS = 4096

# One instruction, reused across providers: transcribe verbatim, keep the original
# script/language, add nothing. Preserving the source language is the whole point —
# the extracted text is embedded and must match how users ask about it.
_PROMPT = (
    "Transcribe all text visible in this image exactly as written, preserving line "
    "breaks and the original language and characters. Do not translate. Output only "
    "the transcribed text with no commentary or headings. If there is no text, output "
    "nothing."
)


class OCRError(Exception):
    """An image could not be transcribed (unreadable image, bad key, network, etc.)."""


def _to_jpeg(data: bytes) -> bytes:
    """Decode any supported image (incl. HEIC) and re-encode it as JPEG bytes.

    The vision APIs reject ``image/heic`` and some other formats; normalizing to JPEG
    up front means every provider receives something it accepts. Raises
    :class:`OCRError` if the bytes are not a readable image."""
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OCRError("Could not read image file.") from exc
    buffer = BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _genai_client(api_key: str) -> genai.Client:
    """Build a Google GenAI client (lazy import; monkeypatched in tests)."""
    from google import genai

    return genai.Client(api_key=api_key)


async def _ocr_gemini(api_key: str, model: str, jpeg: bytes) -> str:
    from google.genai import types

    client = _genai_client(api_key)
    image_part = types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")
    response = await client.aio.models.generate_content(
        model=model,
        # A mixed [Part, str] list is a valid ``contents``; the SDK's union type is
        # list-invariant so mypy can't see it.
        contents=[image_part, _PROMPT],  # type: ignore[arg-type]
        config=types.GenerateContentConfig(max_output_tokens=_OCR_MAX_TOKENS),
    )
    return (response.text or "").strip()


async def _ocr_openai_compatible(
    api_key: str, model: str, jpeg: bytes, base_url: str | None
) -> str:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    data_url = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
    completion = await client.chat.completions.create(
        model=model,
        max_tokens=_OCR_MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    )
    return (completion.choices[0].message.content or "").strip()


async def _ocr_anthropic(api_key: str, model: str, jpeg: bytes) -> str:
    client = AsyncAnthropic(api_key=api_key)
    message = await client.messages.create(
        model=model,
        max_tokens=_OCR_MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(jpeg).decode(),
                        },
                    },
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
    )
    return "".join(b.text for b in message.content if isinstance(b, TextBlock)).strip()


async def transcribe_image(
    provider: str, api_key: str, model: str, *, image: bytes, base_url: str | None = None
) -> str:
    """Transcribe the text in ``image`` using ``provider``'s vision model ``model``.

    ``image`` is raw upload bytes in any supported format (normalized to JPEG here).
    ``base_url`` is used for OpenRouter and custom endpoints. Returns the transcribed
    text (possibly empty when the image has none). Raises :class:`OCRError` for an
    unknown provider or any provider-side failure.
    """
    jpeg = _to_jpeg(image)
    name = provider.lower()
    try:
        if name == "gemini":
            return await _ocr_gemini(api_key, model, jpeg)
        if name == "openai":
            return await _ocr_openai_compatible(api_key, model, jpeg, None)
        if name == "openrouter":
            base = base_url or get_settings().openrouter_base_url
            return await _ocr_openai_compatible(api_key, model, jpeg, base)
        if name == "custom":
            return await _ocr_openai_compatible(api_key, model, jpeg, base_url)
        if name == "anthropic":
            return await _ocr_anthropic(api_key, model, jpeg)
    except OCRError:
        raise
    except Exception as exc:  # noqa: BLE001 — any SDK/network failure becomes a clean error
        raise OCRError(f"Could not transcribe image: {exc}") from exc
    raise OCRError(f"Unsupported provider: {provider!r}")
