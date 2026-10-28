"""Tests for image OCR via the repository's configured vision model.

The provider SDK clients are faked (monkeypatched constructors) so nothing hits the
network. We assert each provider is dispatched, that the transcribed text comes back,
that any input image — including HEIC — is normalized to JPEG before it is sent, and
that a bad image or provider failure surfaces as ``OCRError``.
"""

from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock
from PIL import Image

from contextvault.llm.ocr import OCRError, transcribe_image


def _png(fmt: str = "PNG") -> bytes:
    buf = BytesIO()
    Image.new("RGB", (12, 8), "white").save(buf, format=fmt)
    return buf.getvalue()


class _Recorder(dict[str, Any]):
    """Captures the kwargs/messages a fake client was called with."""


def _fake_openai(recorder: _Recorder, reply: str) -> type:
    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            recorder.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            recorder["base_url"] = kwargs.get("base_url")
            self.chat = SimpleNamespace(completions=_Completions())

    return FakeOpenAI


async def test_openai_transcribes_and_sends_an_image(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(
        "contextvault.llm.ocr.AsyncOpenAI", _fake_openai(recorder, "Оплата 1250 грн")
    )

    text = await transcribe_image("openai", "sk-x", "gpt-4o", image=_png())
    assert text == "Оплата 1250 грн"
    assert recorder["model"] == "gpt-4o"
    # A normalized JPEG image is included in the user message.
    content = recorder["messages"][0]["content"]
    kinds = {part["type"] for part in content}
    assert kinds == {"text", "image_url"}
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_openrouter_passes_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr("contextvault.llm.ocr.AsyncOpenAI", _fake_openai(recorder, "text"))

    await transcribe_image(
        "openrouter", "sk-x", "openai/gpt-4o", image=_png(), base_url="https://router/api"
    )
    assert recorder["base_url"] == "https://router/api"


async def test_custom_passes_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr("contextvault.llm.ocr.AsyncOpenAI", _fake_openai(recorder, "text"))

    await transcribe_image("custom", "sk-noauth", "llava", image=_png(), base_url="http://x/v1")
    assert recorder["base_url"] == "http://x/v1"


async def test_anthropic_transcribes(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()

    class _Messages:
        async def create(self, **kwargs: Any) -> Any:
            recorder.update(kwargs)
            return SimpleNamespace(content=[TextBlock(type="text", text="Привіт", citations=None)])

    class FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            self.messages = _Messages()

    monkeypatch.setattr("contextvault.llm.ocr.AsyncAnthropic", FakeAnthropic)

    text = await transcribe_image("anthropic", "sk-x", "claude-opus-4-8", image=_png())
    assert text == "Привіт"
    # An image block is part of the message content.
    blocks = recorder["messages"][0]["content"]
    assert any(b.get("type") == "image" for b in blocks)


async def test_gemini_transcribes(monkeypatch: pytest.MonkeyPatch) -> None:
    class _AioModels:
        async def generate_content(self, **kwargs: Any) -> Any:
            return SimpleNamespace(text="Ґрунтовний текст")

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            self.aio = SimpleNamespace(models=_AioModels())

    monkeypatch.setattr("contextvault.llm.ocr._genai_client", lambda api_key: FakeClient())

    text = await transcribe_image("gemini", "sk-x", "gemini-2.5-flash", image=_png())
    assert text == "Ґрунтовний текст"


async def test_heic_input_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    # The vision APIs reject HEIC; the OCR layer must normalize it to JPEG first, so a
    # HEIC upload transcribes just like a PNG.
    recorder = _Recorder()
    monkeypatch.setattr("contextvault.llm.ocr.AsyncOpenAI", _fake_openai(recorder, "ok"))

    text = await transcribe_image("openai", "sk-x", "gpt-4o", image=_png(fmt="HEIF"))
    assert text == "ok"
    image_part = next(p for p in recorder["messages"][0]["content"] if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_unreadable_image_raises_ocr_error() -> None:
    with pytest.raises(OCRError, match="Could not read image"):
        await transcribe_image("openai", "sk-x", "gpt-4o", image=b"not an image")


async def test_provider_failure_becomes_ocr_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("boom: bad key")

    monkeypatch.setattr("contextvault.llm.ocr.AsyncOpenAI", FakeOpenAI)
    with pytest.raises(OCRError, match="Could not transcribe image"):
        await transcribe_image("openai", "bad", "gpt-4o", image=_png())


async def test_unknown_provider_raises_ocr_error() -> None:
    with pytest.raises(OCRError, match="Unsupported provider"):
        await transcribe_image("nope", "sk-x", "m", image=_png())
