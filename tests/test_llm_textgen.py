"""Dispatch + error-wrapping contract of the plain-text generation helper."""

from types import SimpleNamespace
from typing import Any

import pytest

import contextvault.llm.textgen as textgen
from contextvault.llm.textgen import TextGenError, generate_text


async def test_unknown_provider_raises() -> None:
    with pytest.raises(TextGenError, match="Unsupported provider"):
        await generate_text("mystery", "k", "m", prompt="hi")


async def test_dispatches_to_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(api_key: str, model: str, prompt: str) -> str:
        assert (api_key, model, prompt) == ("k", "m", "hi")
        return "generated"

    monkeypatch.setattr(textgen, "_generate_gemini", fake)
    assert await generate_text("gemini", "k", "m", prompt="hi") == "generated"


async def test_provider_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(api_key: str, model: str, prompt: str) -> str:
        raise RuntimeError("quota")

    monkeypatch.setattr(textgen, "_generate_gemini", boom)
    with pytest.raises(TextGenError, match="quota"):
        await generate_text("gemini", "k", "m", prompt="hi")


async def test_custom_passes_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder: dict[str, Any] = {}

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            recorder.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="generated"))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            recorder["base_url"] = kwargs.get("base_url")
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setattr(textgen, "AsyncOpenAI", FakeOpenAI)
    result = await generate_text(
        "custom", "sk-noauth", "llama3.1:8b", prompt="hi", base_url="http://x/v1"
    )
    assert result == "generated"
    assert recorder["base_url"] == "http://x/v1"


class TestTransientRetry:
    """A momentary provider 503 used to mark a whole report FAILED, terminally.

    One report is many calls — a plan, up to three SQL attempts per section, then
    prose — and the orchestrator retries only *contract* violations, so a
    transport hiccup anywhere in that fan-out destroyed the report.
    """

    def test_upstream_overload_is_transient(self) -> None:
        assert textgen.is_transient(RuntimeError("503 UNAVAILABLE. high demand"))
        assert textgen.is_transient(RuntimeError("429 Too Many Requests"))
        assert textgen.is_transient(RuntimeError("The model is overloaded"))
        assert textgen.is_transient(TimeoutError())
        assert textgen.is_transient(ConnectionError("connection reset by peer"))

    def test_a_broken_configuration_is_not_transient(self) -> None:
        assert not textgen.is_transient(RuntimeError("401 UNAUTHENTICATED: invalid api key"))
        assert not textgen.is_transient(RuntimeError("400 INVALID_ARGUMENT"))
        assert not textgen.is_transient(RuntimeError("404 model not found"))
        assert not textgen.is_transient(ValueError("something nobody has seen before"))

    def test_a_permanent_code_wins_over_a_transient_word(self) -> None:
        """ "try again" appears in plenty of 401 bodies; the code is what counts."""
        assert not textgen.is_transient(RuntimeError("403 PERMISSION_DENIED, try again"))

    async def test_a_transient_failure_is_retried_and_can_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = 0

        async def flaky(api_key: str, model: str, prompt: str) -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("503 UNAVAILABLE. high demand")
            return "generated"

        slept: list[float] = []

        async def no_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(textgen, "_generate_gemini", flaky)
        monkeypatch.setattr(textgen, "_sleep", no_sleep)
        assert await generate_text("gemini", "k", "m", prompt="hi") == "generated"
        assert attempts == 3
        assert slept == [1.0, 3.0]  # backs off rather than hammering

    async def test_a_permanent_failure_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = 0

        async def refused(api_key: str, model: str, prompt: str) -> str:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("401 UNAUTHENTICATED: invalid api key")

        monkeypatch.setattr(textgen, "_generate_gemini", refused)
        with pytest.raises(TextGenError, match="invalid api key"):
            await generate_text("gemini", "k", "m", prompt="hi")
        assert attempts == 1

    async def test_retries_are_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = 0

        async def always_down(api_key: str, model: str, prompt: str) -> str:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("503 UNAVAILABLE")

        async def no_sleep(seconds: float) -> None:
            return None

        monkeypatch.setattr(textgen, "_generate_gemini", always_down)
        monkeypatch.setattr(textgen, "_sleep", no_sleep)
        with pytest.raises(TextGenError, match="503"):
            await generate_text("gemini", "k", "m", prompt="hi")
        assert attempts == 3


class TestServerRetryDelay:
    def test_the_providers_own_delay_wins_over_the_fixed_backoff(self) -> None:
        """A free-tier 429 answers "retryDelay: 39s"; a 1s backoff never clears it."""
        exc = RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '39s'}")
        assert textgen.retry_after(exc, 1.0) == 39.0

    def test_a_shorter_server_delay_never_shortens_the_backoff(self) -> None:
        exc = RuntimeError("429 {'retryDelay': '0s'}")
        assert textgen.retry_after(exc, 3.0) == 3.0

    def test_a_provider_cannot_park_the_task_indefinitely(self) -> None:
        exc = RuntimeError("429 {'retryDelay': '3600s'}")
        assert textgen.retry_after(exc, 1.0) == 45.0

    def test_no_delay_named_falls_back(self) -> None:
        assert textgen.retry_after(RuntimeError("503 UNAVAILABLE"), 3.0) == 3.0

    async def test_the_retry_loop_uses_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def limited(api_key: str, model: str, prompt: str) -> str:
            raise RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '39s'}")

        slept: list[float] = []

        async def no_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(textgen, "_generate_gemini", limited)
        monkeypatch.setattr(textgen, "_sleep", no_sleep)
        with pytest.raises(TextGenError):
            await generate_text("gemini", "k", "m", prompt="hi")
        assert slept == [39.0, 39.0]
