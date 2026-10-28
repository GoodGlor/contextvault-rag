"""Tests for the shared numbered-chunk citation scheme.

The prompt-build → parse → map machinery lives in one place
(``contextvault.llm.citations``) so every provider — Anthropic, Gemini, and the
OpenAI/OpenRouter providers to come — produces citations identically. These
tests pin that shared unit directly, independent of any vendor SDK: the sources
are numbered ``[1..n]`` in the prompt, and the ``[n]`` markers a model emits map
back to the exact retrieved passage.
"""

import uuid

from contextvault.llm.citations import (
    NOT_IN_VAULT,
    SYSTEM_PROMPT,
    build_user_message,
    format_history,
    format_sources,
    not_in_vault_answer,
    parse_citations,
)
from contextvault.retrieval import RetrievedChunk


def _chunk(ordinal: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        ordinal=ordinal,
        content=f"passage {ordinal}",
        char_start=ordinal * 100,
        char_end=ordinal * 100 + 50,
        score=0.9,
    )


def test_system_prompt_states_the_grounding_contract() -> None:
    system = SYSTEM_PROMPT.lower()
    # Answer only from the sources, cite by number, be honest when they fall short.
    assert "only" in system
    assert "not in this vault" in system


def test_not_in_vault_message_is_non_empty() -> None:
    assert NOT_IN_VAULT.strip()


def test_not_in_vault_answer_is_the_flagged_honest_refusal() -> None:
    """The shared short-circuit result: canonical text, no citations, flag set."""
    answer = not_in_vault_answer()
    assert answer.text == NOT_IN_VAULT
    assert answer.citations == []
    assert answer.not_in_vault is True


def test_format_sources_numbers_chunks_from_one_in_order() -> None:
    text = format_sources([_chunk(0), _chunk(1)])
    assert "[1] passage 0" in text
    assert "[2] passage 1" in text
    assert text.index("[1]") < text.index("[2]")


def test_build_user_message_carries_numbered_sources_and_question() -> None:
    message = build_user_message("what is x?", [_chunk(0), _chunk(1)])
    assert "[1]" in message and "passage 0" in message
    assert "[2]" in message and "passage 1" in message
    assert "what is x?" in message


def test_build_user_message_without_history_has_no_conversation_preamble() -> None:
    message = build_user_message("what is x?", [_chunk(0)])
    assert "Conversation so far" not in message


def test_system_prompt_forbids_treating_history_as_a_source() -> None:
    system = SYSTEM_PROMPT.lower()
    # History is context for interpreting the question, never a citable source.
    assert "earlier" in system
    assert "never treat an earlier answer as a source" in system


def test_format_history_renders_alternating_user_assistant_lines() -> None:
    text = format_history([("what is PTO?", "20 days [1]."), ("part-timers?", "Pro-rated [2].")])
    assert "User: what is PTO?" in text
    assert "Assistant: 20 days [1]." in text
    # Oldest first, and the follow-up comes after.
    assert text.index("what is PTO?") < text.index("part-timers?")


def test_build_user_message_threads_history_before_sources_and_question() -> None:
    message = build_user_message(
        "and for part-timers?",
        [_chunk(0)],
        history=[("what is the PTO policy?", "Employees accrue 20 days [1].")],
    )
    assert "Conversation so far" in message
    assert "what is the PTO policy?" in message
    # Context precedes the current sources and question.
    assert message.index("Conversation so far") < message.index("Sources:")
    assert message.index("Sources:") < message.index("and for part-timers?")


def test_parse_maps_markers_back_to_exact_source_spans() -> None:
    chunks = [_chunk(0), _chunk(1)]
    citations = parse_citations("A [1]. B [2].", chunks)

    assert [c.number for c in citations] == [1, 2]
    assert [(c.chunk_id, c.source_id, c.char_start, c.char_end) for c in citations] == [
        (chunks[0].chunk_id, chunks[0].source_id, chunks[0].char_start, chunks[0].char_end),
        (chunks[1].chunk_id, chunks[1].source_id, chunks[1].char_start, chunks[1].char_end),
    ]


def test_parse_collapses_repeats_in_first_appearance_order() -> None:
    chunks = [_chunk(0), _chunk(1)]
    citations = parse_citations("B [2]. A [1]. Again [2].", chunks)

    assert [c.number for c in citations] == [2, 1]
    assert [c.chunk_id for c in citations] == [chunks[1].chunk_id, chunks[0].chunk_id]


def test_parse_drops_out_of_range_markers() -> None:
    citations = parse_citations("Fabricated [5] and real [1].", [_chunk(0)])
    assert [c.number for c in citations] == [1]


def test_parse_of_uncited_text_yields_no_citations() -> None:
    assert parse_citations("No markers at all.", [_chunk(0)]) == []
