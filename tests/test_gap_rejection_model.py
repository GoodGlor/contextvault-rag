import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import GapRejection, Repository


async def _repo(db_session: AsyncSession) -> Repository:
    repo = Repository(name="Handbook")
    db_session.add(repo)
    await db_session.flush()
    return repo


async def test_rejection_round_trips(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    db_session.add(
        GapRejection(
            repository_id=repo.id,
            normalized_question="what is the vpn?",
            question="What is the VPN?",
            reason="Out of scope",
            rejected_by=None,
        )
    )
    await db_session.flush()


async def test_unique_per_repo_and_normalized_question(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    db_session.add(
        GapRejection(repository_id=repo.id, normalized_question="q", question="Q", reason="r")
    )
    await db_session.flush()
    db_session.add(
        GapRejection(repository_id=repo.id, normalized_question="q", question="Q", reason="r2")
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
