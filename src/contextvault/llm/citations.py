"""The shared numbered-chunk citation scheme: prompt → parse → map.

Only Claude offers native citations, so ContextVault builds its own scheme that
works identically across *every* provider. This module is the one place that
machinery lives; the Anthropic, Gemini, and future OpenAI/OpenRouter providers
all import it, so citations behave the same no matter which vendor generated the
answer:

- **prompt** — ``build_user_message`` lays the retrieved chunks out under
  ``[1..n]`` markers, and ``SYSTEM_PROMPT`` tells the model to answer *only* from
  those numbered sources and cite them by number.
- **parse → map** — ``parse_citations`` reads the ``[n]`` markers back out of the
  model's answer and resolves each to the exact source span (document + character
  offsets) it points at, so the UI can jump to and highlight the passage.

``NOT_IN_VAULT`` is the honest answer a provider returns, without an API call,
when retrieval surfaced nothing relevant.
"""

import re
from collections.abc import Sequence

from contextvault.llm.base import Answer, Citation
from contextvault.retrieval import RetrievedChunk

# Grounding contract handed to the model. Kept blunt on purpose: answer only from
# the numbered sources, cite them by number, and be honest when they fall short.
SYSTEM_PROMPT = (
    "You are ContextVault's retrieval assistant. Answer the user's question using "
    "ONLY the numbered sources provided in their message. Never use outside or "
    "prior knowledge.\n\n"
    "Cite every claim with the bracketed number of the source it draws from — "
    "e.g. [1], or [2] — citing multiple sources where they apply.\n\n"
    "Earlier turns of the conversation may be included for context. Use them only "
    "to understand what the current question refers to (e.g. resolving 'it' or "
    "'that'); never treat an earlier answer as a source, and cite only the numbered "
    "sources below.\n\n"
    "If the sources do not contain the answer, say plainly that the answer is not "
    "in this vault. Do not answer from your own knowledge, and do not invent "
    "citations."
)

# Returned verbatim when retrieval found nothing relevant (no chunks) — the
# honest "not in this vault" answer, produced without consulting the model.
NOT_IN_VAULT = "I don't have anything on that in this repository."

# A citation marker in the model's answer text: ``[1]``, ``[2]``, …
_MARKER = re.compile(r"\[(\d+)\]")


def not_in_vault_answer() -> Answer:
    """The shared honest "not in this vault" result: no API call, no citations.

    Providers return this the moment retrieval hands them no chunks, so the
    refusal text and its ``not_in_vault`` flag are identical across every vendor.
    """
    return Answer(text=NOT_IN_VAULT, citations=[], not_in_vault=True)


def format_sources(chunks: Sequence[RetrievedChunk]) -> str:
    """Lay the chunks out as ``[n] <content>`` blocks, numbered from 1."""
    return "\n\n".join(f"[{i}] {chunk.content}" for i, chunk in enumerate(chunks, start=1))


def format_history(history: Sequence[tuple[str, str]]) -> str:
    """Lay prior turns out as alternating ``User:`` / ``Assistant:`` lines."""
    return "\n".join(f"User: {question}\nAssistant: {answer}" for question, answer in history)


def build_user_message(
    question: str,
    chunks: Sequence[RetrievedChunk],
    history: Sequence[tuple[str, str]] = (),
) -> str:
    """Compose the user turn: optional conversation context, the numbered sources,
    then the question.

    ``history`` is prior ``(question, answer)`` exchanges, oldest first. When
    present it is rendered as a "Conversation so far" preamble so the model can
    resolve references in a follow-up question — it is context only, never a source
    (see ``SYSTEM_PROMPT``).
    """
    parts: list[str] = []
    if history:
        parts.append(f"Conversation so far:\n{format_history(history)}")
    parts.append(f"Sources:\n{format_sources(chunks)}")
    parts.append(f"Question: {question}")
    return "\n\n".join(parts)


def parse_citations(text: str, chunks: Sequence[RetrievedChunk]) -> list[Citation]:
    """Map the ``[n]`` markers in ``text`` back to their source chunks.

    Markers are taken in first-appearance order; repeats collapse to one citation
    and markers outside ``1..len(chunks)`` (a fabricated or mis-numbered ``[n]``)
    are dropped, so a citation always resolves to a real retrieved passage.
    """
    citations: dict[int, Citation] = {}
    order: list[int] = []
    for match in _MARKER.finditer(text):
        number = int(match.group(1))
        if number in citations or not 1 <= number <= len(chunks):
            continue
        chunk = chunks[number - 1]
        citations[number] = Citation(
            number=number,
            chunk_id=chunk.chunk_id,
            source_id=chunk.source_id,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
        )
        order.append(number)
    return [citations[number] for number in order]
