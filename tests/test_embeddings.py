"""Tests for the Gemini-backed embedding provider.

A fake genai client stands in for the network, so the suite runs offline and never
touches torch or a real API key.
"""

from collections.abc import Sequence
from typing import Any

import pytest

import contextvault.embeddings.gemini as gemini_mod
from contextvault.embeddings import EmbeddingProvider, GeminiEmbeddingProvider
from contextvault.embeddings.gemini import EmbeddingError


class _FakeEmbedding:
    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeResponse:
    def __init__(self, embeddings: list[_FakeEmbedding]) -> None:
        self.embeddings = embeddings


class _FakeModels:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self._recorder = recorder

    def embed_content(self, *, model: str, contents: Sequence[str], config: Any) -> _FakeResponse:
        self._recorder.setdefault("calls", []).append(
            {"model": model, "contents": list(contents), "config": config}
        )
        # Return one 3-vector per input, non-unit so normalization is observable.
        return _FakeResponse([_FakeEmbedding([3.0, 0.0, 4.0]) for _ in contents])


class _FakeClient:
    def __init__(self, recorder: dict[str, Any]) -> None:
        self.models = _FakeModels(recorder)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    rec: dict[str, Any] = {}
    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: _FakeClient(rec))
    return rec


def test_provider_satisfies_protocol() -> None:
    provider: EmbeddingProvider = GeminiEmbeddingProvider(
        api_key="k", model_name="gemini-embedding-001", dimension=1024
    )
    assert isinstance(provider, EmbeddingProvider)
    assert provider.dimension == 1024


def test_embed_normalizes_vectors(recorder: dict[str, Any]) -> None:
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    vectors = provider.embed(["hello", "привіт"])
    assert len(vectors) == 2
    # [3,0,4] has norm 5 → normalized to [0.6, 0, 0.8]
    assert vectors[0] == pytest.approx([0.6, 0.0, 0.8])


def test_embed_passes_dimension_and_document_task(recorder: dict[str, Any]) -> None:
    provider = GeminiEmbeddingProvider(
        api_key="k", model_name="gemini-embedding-001", dimension=1024
    )
    provider.embed(["a"])
    config = recorder["calls"][0]["config"]
    assert config.output_dimensionality == 1024
    assert config.task_type == "RETRIEVAL_DOCUMENT"
    assert recorder["calls"][0]["model"] == "gemini-embedding-001"


def test_embed_query_task(recorder: dict[str, Any]) -> None:
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    provider.embed(["q"], task="query")
    assert recorder["calls"][0]["config"].task_type == "RETRIEVAL_QUERY"


def test_embed_batches_and_preserves_order(recorder: dict[str, Any]) -> None:
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    texts = [str(i) for i in range(250)]  # > 2 batches of 100
    vectors = provider.embed(texts)
    assert len(vectors) == 250
    calls = recorder["calls"]
    assert [len(c["contents"]) for c in calls] == [100, 100, 50]


def test_embed_empty_returns_empty(recorder: dict[str, Any]) -> None:
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    assert provider.embed([]) == []
    assert "calls" not in recorder


def test_embed_wraps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        @property
        def models(self) -> Any:
            raise RuntimeError("network down")

    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: _Boom())
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    with pytest.raises(EmbeddingError, match="network down"):
        provider.embed(["a"])


def test_embed_wraps_unknown_task(recorder: dict[str, Any]) -> None:
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    with pytest.raises(EmbeddingError):
        provider.embed(["a"], task="bogus")  # type: ignore[arg-type]


def test_embed_raises_when_gemini_returns_no_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoneModels:
        def embed_content(
            self, *, model: str, contents: Sequence[str], config: Any
        ) -> _FakeResponse:
            return _FakeResponse(None)  # type: ignore[arg-type]

    class _NoneClient:
        models = _NoneModels()

    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: _NoneClient())
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    with pytest.raises(EmbeddingError, match="no embeddings"):
        provider.embed(["a"])


class _RateLimited(Exception):
    """Stands in for the SDK's 429; mirrors the real message and ``code`` attribute."""

    def __init__(self, message: str = "429 RESOURCE_EXHAUSTED. You exceeded your quota") -> None:
        super().__init__(message)
        self.code = 429


class _FlakyModels:
    """Raises ``error`` for the first ``failures`` calls, then embeds normally."""

    def __init__(self, recorder: dict[str, Any], failures: int, error: Exception) -> None:
        self._recorder = recorder
        self._remaining = failures
        self._error = error

    def embed_content(self, *, model: str, contents: Sequence[str], config: Any) -> _FakeResponse:
        self._recorder.setdefault("calls", []).append({"contents": list(contents)})
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return _FakeResponse([_FakeEmbedding([3.0, 0.0, 4.0]) for _ in contents])


@pytest.fixture(autouse=True)
def fresh_pacer() -> None:
    """The request pacer is process-wide; clear it so tests do not pace each other."""
    gemini_mod.reset_request_pacer()


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture backoff/pacing delays instead of really sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr(gemini_mod, "_sleep", recorded.append)
    return recorded


def _flaky_provider(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failures: int,
    error: Exception,
    **kwargs: Any,
) -> tuple[GeminiEmbeddingProvider, dict[str, Any]]:
    rec: dict[str, Any] = {}
    models = _FlakyModels(rec, failures, error)
    client = type("_C", (), {"models": models})()
    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: client)
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024, **kwargs)
    return provider, rec


def test_embed_retries_after_rate_limit_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A 429 is transient: back off and retry rather than failing the document."""
    provider, rec = _flaky_provider(monkeypatch, failures=2, error=_RateLimited(), max_retries=5)
    vectors = provider.embed(["a", "b"])
    assert len(vectors) == 2
    assert vectors[0] == pytest.approx([0.6, 0.0, 0.8])
    assert len(rec["calls"]) == 3  # two rejected attempts, then success
    assert len(sleeps) == 2


def test_embed_backoff_grows_exponentially(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    provider, _ = _flaky_provider(
        monkeypatch, failures=3, error=_RateLimited(), max_retries=5, retry_base_delay=2.0
    )
    provider.embed(["a"])
    # Each wait is base * 2**attempt, scaled by jitter in [0.5, 1.0].
    for attempt, delay in enumerate(sleeps):
        ceiling = 2.0 * (2**attempt)
        assert ceiling * 0.5 <= delay <= ceiling


def test_embed_gives_up_after_max_retries(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    provider, rec = _flaky_provider(monkeypatch, failures=99, error=_RateLimited(), max_retries=3)
    with pytest.raises(EmbeddingError, match="RESOURCE_EXHAUSTED"):
        provider.embed(["a"])
    assert len(rec["calls"]) == 4  # 1 initial attempt + 3 retries
    assert len(sleeps) == 3


def test_embed_detects_rate_limit_from_message_without_code(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The SDK does not always attach ``code``; the 429 text alone must still retry."""
    error = RuntimeError("429 RESOURCE_EXHAUSTED. You exceeded your current quota")
    provider, rec = _flaky_provider(monkeypatch, failures=1, error=error, max_retries=5)
    provider.embed(["a"])
    assert len(rec["calls"]) == 2


def test_embed_does_not_retry_non_rate_limit_errors(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A bad key or malformed request will never succeed — fail fast, do not sit in backoff."""
    provider, rec = _flaky_provider(
        monkeypatch, failures=99, error=RuntimeError("400 API key not valid"), max_retries=5
    )
    with pytest.raises(EmbeddingError, match="API key not valid"):
        provider.embed(["a"])
    assert len(rec["calls"]) == 1
    assert sleeps == []


def test_embed_honors_server_retry_delay(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """When Gemini says how long to wait, that beats our guess."""
    error = _RateLimited("429 RESOURCE_EXHAUSTED {'retryDelay': '17s'}")
    provider, _ = _flaky_provider(monkeypatch, failures=1, error=error, max_retries=5)
    provider.embed(["a"])
    assert sleeps == [17.0]


def test_embed_paces_successive_batches(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Back-to-back batches burn the per-minute quota; space them out."""
    provider, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=40
    )
    provider.embed([str(i) for i in range(250)])  # 3 batches → 2 gaps
    assert len(sleeps) == 2
    # 40 requests/min is one every 1.5s. Time the previous request already took
    # counts toward the interval, so each wait is 1.5s minus a sliver of elapsed time.
    assert all(delay == pytest.approx(1.5, abs=0.01) for delay in sleeps)


def test_pacing_is_shared_across_provider_instances(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """The quota is per project, not per object.

    Each upload builds its own provider via the request dependency, so pacing held
    on the instance let N concurrent ingestions send N times the intended rate —
    which is exactly how a 100-requests-per-minute ceiling got blown.
    """
    first, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=40
    )
    second, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=40
    )
    first.embed(["a"])
    assert sleeps == []
    second.embed(["b"])  # a different instance must still wait its turn
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.5, abs=0.01)


def test_requests_per_minute_sets_the_interval(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    provider, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=120
    )
    provider.embed([str(i) for i in range(200)])  # 2 batches → 1 gap
    assert sleeps[0] == pytest.approx(0.5, abs=0.01)


def test_pacing_disabled_when_rate_is_unset(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """0 means 'do not pace' — an escape hatch for a paid tier with headroom."""
    provider, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=0
    )
    provider.embed([str(i) for i in range(250)])
    assert sleeps == []


def test_embed_paces_across_separate_calls(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """Quota is per key, not per call: ingestion embeds in slices to report progress,
    and those slices must not burst just because each is its own ``embed()``."""
    provider, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=40
    )
    provider.embed(["a"])
    assert sleeps == []  # first request of the first call waits for nothing
    provider.embed(["b"])
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(1.5, abs=0.01)


def test_embed_does_not_pace_a_single_batch(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    provider, _ = _flaky_provider(
        monkeypatch, failures=0, error=_RateLimited(), requests_per_minute=40
    )
    provider.embed(["a"])
    assert sleeps == []


def test_embed_retry_does_not_duplicate_earlier_batches(
    monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    """A late 429 must retry only the failing batch, not re-embed what already succeeded."""

    class _FailSecondBatch:
        def __init__(self, recorder: dict[str, Any]) -> None:
            self._recorder = recorder
            self._failed = False

        def embed_content(
            self, *, model: str, contents: Sequence[str], config: Any
        ) -> _FakeResponse:
            self._recorder.setdefault("calls", []).append(list(contents))
            if len(self._recorder["calls"]) == 2 and not self._failed:
                self._failed = True
                raise _RateLimited()
            return _FakeResponse([_FakeEmbedding([3.0, 0.0, 4.0]) for _ in contents])

    rec: dict[str, Any] = {}
    client = type("_C", (), {"models": _FailSecondBatch(rec)})()
    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: client)
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)

    vectors = provider.embed([str(i) for i in range(150)])
    assert len(vectors) == 150
    # Batch 1 sent once; batch 2 sent twice (rejected, then retried).
    assert [len(c) for c in rec["calls"]] == [100, 50, 50]


def test_embed_raises_when_embedding_has_no_values(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoValuesModels:
        def embed_content(
            self, *, model: str, contents: Sequence[str], config: Any
        ) -> _FakeResponse:
            return _FakeResponse([_FakeEmbedding(None)])  # type: ignore[arg-type]

    class _NoValuesClient:
        models = _NoValuesModels()

    monkeypatch.setattr(gemini_mod, "_genai_client", lambda api_key: _NoValuesClient())
    provider = GeminiEmbeddingProvider(api_key="k", model_name="m", dimension=1024)
    with pytest.raises(EmbeddingError, match="empty embedding"):
        provider.embed(["a"])
