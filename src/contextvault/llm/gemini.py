"""The Google (Gemini) ``LLMProvider`` — generation via the Google GenAI SDK.

Implements the provider-agnostic ``answer`` contract against Gemini,
enforcing the same two defining behaviours as the Anthropic provider:

- **Grounded, numbered-chunk answers.** The retrieved chunks are laid out under
  ``[1..n]`` markers and the system instruction tells the model to answer *only*
  from them and cite those numbers. The ``[n]`` markers the model emits are
  parsed back into ``Citation`` objects pointing at the exact source span, so the
  citation experience is identical across every provider — no reliance on any
  vendor-native citation feature.
- **Honest "not in this vault".** With no relevant chunks the provider
  short-circuits and returns the honest answer without spending an API call.

The prompt-build / marker-parse machinery is the shared
``contextvault.llm.citations`` scheme; this provider only wires it to
the Google GenAI SDK. The model defaults to ``gemini-2.5-flash`` and is
configurable (constructor arg or ``gemini_model`` setting), per the card.
"""

from collections.abc import Sequence

from google import genai
from google.genai import types

from contextvault.core.config import get_settings
from contextvault.llm.base import Answer
from contextvault.llm.citations import (
    SYSTEM_PROMPT,
    build_user_message,
    not_in_vault_answer,
    parse_citations,
)
from contextvault.retrieval import RetrievedChunk


class GeminiLLMProvider:
    """``LLMProvider`` backed by Gemini via the Google GenAI SDK."""

    def __init__(
        self,
        *,
        client: genai.Client | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """Configure the provider.

        ``client`` injects a ``genai.Client`` (mainly for tests); otherwise one is
        built from the per-repository ``api_key`` (each repository carries its own
        encrypted key — there is no process-wide key fallback). ``model`` and
        ``max_tokens`` default to the ``gemini_model`` / ``llm_max_tokens`` settings.
        """
        settings = get_settings()
        self._model = model or settings.gemini_model
        self._max_tokens = max_tokens or settings.llm_max_tokens
        self._client = client or genai.Client(api_key=api_key)

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        """Generate a grounded, cited answer to ``question`` from ``chunks``.

        With no chunks, returns the honest "not in this vault" answer without an
        API call. Otherwise numbers the chunks, asks Gemini to answer only from
        them, and resolves the ``[n]`` markers in the reply to ``Citation``s.
        ``history`` (prior turns) is passed as conversation context only.
        """
        if not chunks:
            return not_in_vault_answer()

        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=build_user_message(question, chunks, history),
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=self._max_tokens,
            ),
        )
        # ``response.text`` concatenates the text parts; it is None when the model
        # returned no text (e.g. a safety-blocked response).
        text = (response.text or "").strip()
        citations = parse_citations(text, chunks)
        # Citations are the proof of grounding: an answer that cites none of the
        # sources grounds nothing, so flag it honestly rather than pass it off.
        return Answer(text=text, citations=citations, not_in_vault=not citations)
