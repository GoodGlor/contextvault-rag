"""The OpenAI (ChatGPT) ``LLMProvider`` — generation via the OpenAI SDK.

Implements the provider-agnostic ``answer`` contract against OpenAI's
Chat Completions API, enforcing the same two defining behaviours as the Anthropic
and Gemini providers:

- **Grounded, numbered-chunk answers.** The retrieved chunks are laid out under
  ``[1..n]`` markers and the system message tells the model to answer *only* from
  them and cite those numbers. The ``[n]`` markers the model emits are parsed back
  into ``Citation`` objects pointing at the exact source span, so the citation
  experience is identical across every provider — no reliance on any vendor-native
  citation feature.
- **Honest "not in this vault".** With no relevant chunks the provider
  short-circuits and returns the honest answer without spending an API call.

The prompt-build / marker-parse machinery is the shared
``contextvault.llm.citations`` scheme; this provider only wires it to
the OpenAI SDK. The model defaults to ``gpt-4o`` and is configurable (constructor
arg or ``openai_model`` setting), per the card.
"""

from collections.abc import Sequence

from openai import AsyncOpenAI

from contextvault.core.config import get_settings
from contextvault.llm.base import Answer
from contextvault.llm.citations import (
    SYSTEM_PROMPT,
    build_user_message,
    not_in_vault_answer,
    parse_citations,
)
from contextvault.retrieval import RetrievedChunk


class OpenAILLMProvider:
    """``LLMProvider`` backed by ChatGPT via the OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        client: AsyncOpenAI | None = None,
        api_key: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        base_url: str | None = None,
    ) -> None:
        """Configure the provider.

        ``client`` injects an ``AsyncOpenAI`` (mainly for tests); otherwise one is
        built from the per-repository ``api_key`` (each repository carries its own
        encrypted key — there is no process-wide key fallback). ``model`` and
        ``max_tokens`` default to the ``openai_model`` / ``llm_max_tokens`` settings.
        ``base_url`` aims the client at a custom OpenAI-compatible endpoint (e.g. a
        self-hosted server); it defaults to the SDK's OpenAI endpoint when ``None``.
        """
        settings = get_settings()
        self._model = model or settings.openai_model
        self._max_tokens = max_tokens or settings.llm_max_tokens
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def answer(
        self,
        question: str,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[tuple[str, str]] = (),
    ) -> Answer:
        """Generate a grounded, cited answer to ``question`` from ``chunks``.

        With no chunks, returns the honest "not in this vault" answer without an
        API call. Otherwise numbers the chunks, asks the model to answer only from
        them, and resolves the ``[n]`` markers in the reply to ``Citation``s.
        ``history`` (prior turns) is passed as conversation context only.
        """
        if not chunks:
            return not_in_vault_answer()

        completion = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_message(question, chunks, history)},
            ],
        )
        # ``message.content`` is None when the model returned no text (e.g. a
        # refusal or a filtered response).
        text = (completion.choices[0].message.content or "").strip()
        citations = parse_citations(text, chunks)
        # Citations are the proof of grounding: an answer that cites none of the
        # sources grounds nothing, so flag it honestly rather than pass it off.
        return Answer(text=text, citations=citations, not_in_vault=not citations)
