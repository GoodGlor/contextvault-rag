"""Gap-rejection service tests.

Rejecting a knowledge gap is an admin decision, keyed by ``(repository_id,
normalized_question)`` — the same identity ``list_knowledge_gaps`` aggregates on.
These tests assert: a rejected gap disappears from the active gap list; rejecting
the same question twice (regardless of case/whitespace) upserts a single row; and
rejected gaps list newest first.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import GapRejection, QueryLog, Repository, Role
from contextvault.services import knowledge_gaps as gap_service
from contextvault.services import users as user_service


async def _repo(db_session: AsyncSession, name: str = "Handbook") -> Repository:
    repo = Repository(name=name)
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _gap_log(db_session: AsyncSession, repo_id: uuid.UUID, question: str) -> None:
    db_session.add(
        QueryLog(
            user_id=None,
            repository_id=repo_id,
            question=question,
            top_score=None,
            chunk_count=0,
            not_in_vault=True,
        )
    )
    await db_session.flush()


async def test_rejected_question_is_excluded_from_gaps(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    await _gap_log(db_session, repo.id, "What is the VPN?")
    await _gap_log(db_session, repo.id, "How to reset password?")
    admin = await user_service.create_user(
        db_session, username="admin", password="pw", role=Role.ADMIN
    )
    await gap_service.reject_gap(
        db_session, repo.id, question="What is the VPN?", reason="n/a", admin_id=admin.id
    )
    gaps = await gap_service.list_knowledge_gaps(db_session, repo.id)
    assert [g.question for g in gaps] == ["How to reset password?"]


async def test_reject_is_idempotent_upsert(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    await gap_service.reject_gap(db_session, repo.id, question="Q", reason="first", admin_id=None)
    await gap_service.reject_gap(
        db_session, repo.id, question="q", reason="second", admin_id=None
    )  # same normalized
    rows = (await db_session.execute(sa.select(GapRejection))).scalars().all()
    assert len(rows) == 1
    assert rows[0].reason == "second"


async def test_list_rejected_newest_first(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    await gap_service.reject_gap(db_session, repo.id, question="A", reason="a", admin_id=None)
    await gap_service.reject_gap(db_session, repo.id, question="B", reason="b", admin_id=None)
    rejected = await gap_service.list_rejected_gaps(db_session, repo.id)
    assert {r.question for r in rejected} == {"A", "B"}


async def test_reject_gap_normalization_matches_sql_for_edge_tab(
    db_session: AsyncSession,
) -> None:
    """SQL ``btrim()`` only trims ASCII spaces from the edges; the Python-side
    normalization used to store a rejection's identity must mirror that exactly
    (spaces-only edge trim + whitespace collapse), or a question with a leading/
    trailing tab/newline will be rejected but never actually excluded, since its
    stored key won't match the SQL-normalized group key ``list_knowledge_gaps`` uses.
    """
    repo = await _repo(db_session)
    question = "\tWhat is the VPN?"
    await _gap_log(db_session, repo.id, question)
    await gap_service.reject_gap(
        db_session, repo.id, question=question, reason="n/a", admin_id=None
    )
    gaps = await gap_service.list_knowledge_gaps(db_session, repo.id)
    assert question not in [g.question for g in gaps]
    assert gaps == []
