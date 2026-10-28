"""The OpenRouter ``LLMProvider`` — an OpenAI-compatible gateway to many models.

OpenRouter exposes the OpenAI Chat Completions wire format, so the entire request
shape and the grounded numbered-chunk / ``[n]``-citation machinery of the OpenAI
provider are reused verbatim: this provider subclasses
``OpenAILLMProvider`` and only re-aims the client. It points an ``AsyncOpenAI`` at
OpenRouter's ``base_url`` and authenticates with the OpenRouter key, so a single
implementation reaches hundreds of models ("OpenAI reused for
OpenRouter, OpenAI-compatible wire format").

Model ids are vendor-namespaced (``openai/gpt-4o``, ``anthropic/claude-3.5-sonnet``,
…); the default is ``openai/gpt-4o`` and is configurable (constructor arg or the
``openrouter_model`` setting).
"""

from openai import AsyncOpenAI

from contextvault.core.config import get_settings
from contextvault.llm.openai import OpenAILLMProvider


class OpenRouterLLMProvider(OpenAILLMProvider):
    """``LLMProvider`` reaching OpenRouter through the OpenAI-compatible API.

    Inherits ``answer`` (and thus the honest "not in this vault" short-circuit and
    the numbered-chunk citation mapping) from ``OpenAILLMProvider`` unchanged; only
    client construction differs — a different base URL, key, and default model.
    """

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
        built pointing at ``base_url`` (falling back to ``openrouter_base_url``) and
        authenticated with the per-repository ``api_key`` (each repository carries its
        own encrypted key — there is no process-wide key fallback). ``model`` and
        ``max_tokens`` default to the ``openrouter_model`` / ``llm_max_tokens`` settings.
        """
        settings = get_settings()
        resolved_client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or settings.openrouter_base_url,
        )
        super().__init__(
            client=resolved_client,
            model=model or settings.openrouter_model,
            max_tokens=max_tokens,
        )
