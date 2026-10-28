"""Embeddings via Google's Gemini embedding API.

Replaces the removed local sentence-transformers model: document/image text and
queries are embedded by Gemini using the global Gemini provider key (the same key the
OCR path uses). No ML model runs on the host — a stateless HTTPS call — so concurrent
ingestion no longer contends on a GPU. Vectors are L2-normalized here because retrieval
treats cosine similarity as a dot product and Gemini does not normalize when a
non-native ``output_dimensionality`` is requested.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from google.genai import types

from contextvault.core.config import get_settings
from contextvault.embeddings.base import EmbedTask

if TYPE_CHECKING:
    from google import genai

# Gemini's asymmetric retrieval task types: documents and queries embed differently.
_TASK_TYPES: dict[str, str] = {
    "document": "RETRIEVAL_DOCUMENT",
    "query": "RETRIEVAL_QUERY",
}

# Cap on inputs per ``embed_content`` call; batch under it.
_BATCH_SIZE = 100

# Marks of a spent quota in an SDK error. The SDK does not always expose a numeric
# ``code``, so the message is checked too — both forms carry the same signal.
_RATE_LIMIT_MARKERS = ("RESOURCE_EXHAUSTED", "429")

# Gemini attaches a ``RetryInfo`` telling us how long to wait, e.g. ``'retryDelay': '38s'``.
# It arrives inside the stringified error detail, so it is read back out by pattern.
_RETRY_DELAY_RE = re.compile(r"['\"]?retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s")

# Indirected so tests can observe waits without really sleeping.
_sleep = time.sleep


class EmbeddingError(Exception):
    """A Gemini embedding request failed (bad key, network, quota, etc.)."""


def _genai_client(api_key: str) -> genai.Client:
    """Build a Gemini client (lazy import; monkeypatched in tests)."""
    from google import genai

    return genai.Client(api_key=api_key)


def _is_rate_limited(exc: Exception) -> bool:
    """True when ``exc`` is a spent-quota 429 — the one failure worth retrying."""
    if getattr(exc, "code", None) == 429:
        return True
    message = str(exc)
    return any(marker in message for marker in _RATE_LIMIT_MARKERS)


def _server_retry_delay(exc: Exception) -> float | None:
    """The server's own wait hint, if it sent one."""
    match = _RETRY_DELAY_RE.search(str(exc))
    return float(match.group(1)) if match else None


class _RequestPacer:
    """Process-wide spacing between embedding requests.

    Gemini meters ``embed_content`` per project, not per caller: the free tier's
    quota is ``EmbedContentRequestsPerMinutePerUserPerProjectPerModel`` at 100
    requests/minute. Every concurrent ingestion draws on that one budget, and so
    does query-time embedding, so the pacing has to be shared. Holding it per
    provider instance was not enough — each upload builds its own provider from the
    request dependency, so N simultaneous uploads sent N times the intended rate and
    tripped the ceiling anyway.

    The lock is held across the sleep, which serializes embedding requests by
    design: one global request rate is exactly the point. Requests already run in
    worker threads (``asyncio.to_thread``), so the event loop is never blocked.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._min_interval = 0.0
        self._last_sent_at: float | None = None

    def configure(self, requests_per_minute: int) -> None:
        """Set the rate. Non-positive disables pacing entirely."""
        with self._lock:
            self._min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0

    def acquire(self) -> None:
        """Block until the next request may be sent."""
        with self._lock:
            if self._min_interval > 0.0 and self._last_sent_at is not None:
                # Time the previous request already took counts toward the interval.
                shortfall = self._min_interval - (time.monotonic() - self._last_sent_at)
                if shortfall > 0:
                    _sleep(shortfall)
            self._last_sent_at = time.monotonic()


_request_pacer = _RequestPacer()


def reset_request_pacer() -> None:
    """Forget the last-request timestamp. For tests, which must not pace each other."""
    global _request_pacer
    _request_pacer = _RequestPacer()


def _l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return [float(v) for v in vector]
    return [float(v) / norm for v in vector]


class GeminiEmbeddingProvider:
    """``EmbeddingProvider`` backed by Gemini's embedding API."""

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str,
        dimension: int,
        max_retries: int | None = None,
        retry_base_delay: float | None = None,
        retry_max_delay: float | None = None,
        requests_per_minute: int | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key
        self._model_name = model_name
        self._dimension = dimension
        self._max_retries: int = (
            settings.embedding_max_retries if max_retries is None else max_retries
        )
        self._retry_base_delay: float = (
            settings.embedding_retry_base_delay if retry_base_delay is None else retry_base_delay
        )
        self._retry_max_delay: float = (
            settings.embedding_retry_max_delay if retry_max_delay is None else retry_max_delay
        )
        # Pacing itself is process-wide (see ``_RequestPacer``); the provider only
        # tells it what rate to run at.
        _request_pacer.configure(
            settings.embedding_requests_per_minute
            if requests_per_minute is None
            else requests_per_minute
        )

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, texts: Sequence[str], *, task: EmbedTask = "document") -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        try:
            config = types.EmbedContentConfig(
                task_type=_TASK_TYPES[task],
                output_dimensionality=self._dimension,
            )
            client = _genai_client(self._api_key)
            for start in range(0, len(texts), _BATCH_SIZE):
                batch = list(texts[start : start + _BATCH_SIZE])
                _request_pacer.acquire()
                embeddings = self._embed_batch(client, batch, config)
                for item in embeddings:
                    if item.values is None:
                        raise EmbeddingError("Gemini returned an empty embedding.")
                    vectors.append(_l2_normalize(item.values))
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 — any SDK/network failure becomes a clean error
            raise EmbeddingError(f"Could not embed text: {exc}") from exc
        return vectors

    def _embed_batch(
        self, client: genai.Client, batch: list[str], config: types.EmbedContentConfig
    ) -> list[types.ContentEmbedding]:
        """Embed one batch, retrying while Gemini reports a spent quota.

        Only this batch is retried — vectors from earlier batches are already held by
        the caller, so a 429 late in a long document costs one request, not the whole
        re-embed. Non-quota failures (bad key, malformed request) will never succeed
        on a retry and propagate immediately.
        """
        for attempt in range(self._max_retries + 1):
            try:
                response = client.models.embed_content(
                    model=self._model_name,
                    contents=batch,  # type: ignore[arg-type]
                    config=config,
                )
            except Exception as exc:  # noqa: BLE001 — narrowed by _is_rate_limited below
                if attempt == self._max_retries or not _is_rate_limited(exc):
                    raise
                _sleep(self._backoff_delay(attempt, exc))
                continue
            embeddings = response.embeddings
            if embeddings is None:
                raise EmbeddingError("Gemini returned no embeddings.")
            return list(embeddings)
        raise AssertionError("unreachable: the loop either returns or raises")

    def _backoff_delay(self, attempt: int, exc: Exception) -> float:
        """How long to wait before retry ``attempt``, counting from zero.

        The server's own ``retryDelay`` wins when present — it knows when the quota
        window actually reopens. Otherwise back off exponentially, jittered so that
        concurrent ingests (each upload runs its own background task) do not all
        wake up and retry in the same instant.
        """
        server_delay = _server_retry_delay(exc)
        if server_delay is not None:
            return server_delay
        # ``2.0`` not ``2``: mypy widens ``int ** int`` to Any (negative exponents yield floats).
        ceiling = min(self._retry_base_delay * (2.0**attempt), self._retry_max_delay)
        return ceiling * random.uniform(0.5, 1.0)
