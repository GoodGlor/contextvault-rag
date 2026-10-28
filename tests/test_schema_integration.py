"""End-to-end schema tests against a real (pgvector) database.

Skips automatically when no migrated database is reachable — see conftest.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import (
    Chunk,
    GeneratedReport,
    Grant,
    Repository,
    Role,
    Source,
    SourceKind,
    User,
)


async def test_round_trip_insert_and_vector_column(db_session: AsyncSession) -> None:
    repo = Repository(name="Handbook", description="HR policies")
    admin = User(username="admin", password_hash="x", role=Role.ADMIN)
    db_session.add_all([repo, admin])
    await db_session.flush()

    source = Source(
        repository_id=repo.id,
        kind=SourceKind.DOCUMENT,
        title="Leave policy",
        original_filename="leave.pdf",
        created_by=admin.id,
    )
    db_session.add(source)
    await db_session.flush()

    embedding = [0.1] * get_dim()
    chunk = Chunk(
        source_id=source.id,
        repository_id=repo.id,
        ordinal=0,
        content="You get 25 days of leave.",
        embedding=embedding,
    )
    db_session.add(chunk)
    await db_session.flush()

    loaded = await db_session.get(Chunk, chunk.id)
    assert loaded is not None
    assert loaded.embedding is not None
    assert list(loaded.embedding) == embedding


async def test_access_filtered_retrieval_is_a_single_query(db_session: AsyncSession) -> None:
    """The permission boundary lives in the query: a chunk is only visible to a
    user who holds a grant on its repository."""
    repo = Repository(name="Secret vault")
    granted = User(username="granted", password_hash="x", role=Role.USER)
    outsider = User(username="outsider", password_hash="x", role=Role.USER)
    db_session.add_all([repo, granted, outsider])
    await db_session.flush()

    source = Source(repository_id=repo.id, kind=SourceKind.ADMIN_NOTE, title="Note")
    db_session.add(source)
    await db_session.flush()
    db_session.add(Chunk(source_id=source.id, repository_id=repo.id, ordinal=0, content="secret"))
    db_session.add(Grant(user_id=granted.id, repository_id=repo.id))
    await db_session.flush()

    def visible_chunks(user_id: object) -> sa.Select[tuple[str]]:
        return (
            sa.select(Chunk.content)
            .join(Grant, Grant.repository_id == Chunk.repository_id)
            .where(Grant.user_id == user_id)
        )

    granted_rows = (await db_session.execute(visible_chunks(granted.id))).scalars().all()
    outsider_rows = (await db_session.execute(visible_chunks(outsider.id))).scalars().all()

    assert granted_rows == ["secret"]
    assert outsider_rows == []


async def test_grant_uniqueness_prevents_duplicates(db_session: AsyncSession) -> None:
    repo = Repository(name="Vault")
    user = User(username="dup", password_hash="x", role=Role.USER)
    db_session.add_all([repo, user])
    await db_session.flush()
    db_session.add(Grant(user_id=user.id, repository_id=repo.id))
    await db_session.flush()
    # A SAVEPOINT isolates the expected failure so the outer transaction stays usable.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(Grant(user_id=user.id, repository_id=repo.id))
            await db_session.flush()


async def test_design_columns_default_to_null(db_session: AsyncSession) -> None:
    """Both columns are nullable with no server default.

    There is no backfill: every report that exists today keeps ``design = NULL``
    and renders exactly what it renders now, because NULL resolves to the same
    default 3a already applied to everything.
    """
    repo = Repository(name="Reports", description=None)
    db_session.add(repo)
    await db_session.flush()

    assert repo.report_template is None

    report = GeneratedReport(repository_id=repo.id, prompt="revenue by year")
    db_session.add(report)
    await db_session.flush()

    assert report.design is None


async def test_design_columns_round_trip_jsonb(db_session: AsyncSession) -> None:
    repo = Repository(name="Reports", description=None, report_template={"template": "minimum"})
    db_session.add(repo)
    await db_session.flush()

    report = GeneratedReport(
        repository_id=repo.id, prompt="revenue by year", design={"template": "prezydia"}
    )
    db_session.add(report)
    await db_session.flush()
    report_id, repo_id = report.id, repo.id
    db_session.expire_all()

    reloaded = await db_session.get(GeneratedReport, report_id)
    assert reloaded is not None
    assert reloaded.design == {"template": "prezydia"}
    reloaded_repo = await db_session.get(Repository, repo_id)
    assert reloaded_repo is not None
    assert reloaded_repo.report_template == {"template": "minimum"}


def get_dim() -> int:
    from contextvault.core.config import get_settings

    return get_settings().embedding_dim
