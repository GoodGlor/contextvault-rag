"""LLM generation layer: the provider-agnostic ``answer`` contract.

``LLMProvider`` is the interface every vendor implementation satisfies;
``Answer``/``Citation`` are the shared result schema (answer text + numbered
citations mapping ``[n]`` back to a source span). ``get_llm_provider`` returns the
system-default provider selected by the ``llm_provider`` setting — the seam the
RAG loop generates through, so call sites depend only on the contract.
"""

from contextvault.core.config import get_settings
from contextvault.llm.base import Answer, Citation, LLMProvider

__all__ = ["Answer", "Citation", "LLMProvider", "get_llm_provider"]


def get_llm_provider(
    name: str | None = None,
    *,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> LLMProvider:
    """Return an LLM provider by name (defaults to the ``llm_provider`` setting).

    Providers are imported lazily so importing this package never drags in a
    vendor SDK — only the selected provider's SDK loads. Five providers are wired:
    Google (Gemini), OpenAI (ChatGPT), OpenRouter, Anthropic (Claude), and
    ``custom`` — a self-hosted OpenAI-compatible server reached via ``base_url``.

    ``api_key``, ``model``, and ``base_url`` let per-repo routing
    build each repository's own provider from its stored configuration; when
    ``None`` the provider falls back to the process-wide settings default, so the
    no-argument call still yields the system-default provider.
    """
    provider = (name or get_settings().llm_provider).lower()
    if provider == "gemini":
        from contextvault.llm.gemini import GeminiLLMProvider

        return GeminiLLMProvider(api_key=api_key, model=model)
    if provider == "openai":
        from contextvault.llm.openai import OpenAILLMProvider

        return OpenAILLMProvider(api_key=api_key, model=model)
    if provider == "openrouter":
        from contextvault.llm.openrouter import OpenRouterLLMProvider

        return OpenRouterLLMProvider(api_key=api_key, model=model, base_url=base_url)
    if provider == "anthropic":
        from contextvault.llm.anthropic import AnthropicLLMProvider

        return AnthropicLLMProvider(api_key=api_key, model=model)
    if provider == "custom":
        # A self-hosted OpenAI-compatible server: reuse the OpenAI answer path aimed
        # at the stored base URL. The repo always supplies a model for custom.
        from contextvault.llm.openai import OpenAILLMProvider

        return OpenAILLMProvider(api_key=api_key, model=model, base_url=base_url)
    raise ValueError(f"unsupported or not-yet-wired LLM provider: {provider!r}")
