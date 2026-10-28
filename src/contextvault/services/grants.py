"""Access-grant service.

A grant is a (user, repository) pair with an optional expiry. These helpers own the
grant lifecycle — create/update (idempotent), revoke, and the two read shapes the
API needs: the grants on a repository (admin view) and the repositories a user can
actively reach (the user's repo picker). Query-time enforcement lives in
``retrieval.search`` / the query endpoint; the "active grant" predicate is repeated
there against the same rule (no expiry, or expiry still in the future).
"""

import uuid
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import Grant, Repository


async def grant_access(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    repository_id: uuid.UUID,
    expires_at: datetime | None,
) -> Grant:
    """Grant ``user_id`` access to ``repository_id`` (idempotent).

    Re-granting an existing (user, repository) pair updates its ``expires_at``
    rather than violating the unique constraint — an admin "grant access" action is
    naturally idempotent and doubles as "adjust the expiry".
    """
    existing = (
        await session.execute(
            sa.select(Grant).where(Grant.user_id == user_id, Grant.repository_id == repository_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.expires_at = expires_at
        await session.flush()
        return existing

    grant = Grant(user_id=user_id, repository_id=repository_id, expires_at=expires_at)
    session.add(grant)
    await session.flush()
    return grant


async def revoke_access(
    session: AsyncSession, *, user_id: uuid.UUID, repository_id: uuid.UUID
) -> bool:
    """Revoke a user's grant on a repository. Returns True if a grant was removed."""
    grant = (
        await session.execute(
            sa.select(Grant).where(Grant.user_id == user_id, Grant.repository_id == repository_id)
        )
    ).scalar_one_or_none()
    if grant is None:
        return False
    await session.delete(grant)
    await session.flush()
    return True


async def list_grants_for_repository(
    session: AsyncSession, repository_id: uuid.UUID
) -> Sequence[Grant]:
    """All grants on a repository (including expired ones — the admin sees history)."""
    result = await session.execute(
        sa.select(Grant).where(Grant.repository_id == repository_id).order_by(Grant.created_at)
    )
    return result.scalars().all()


async def has_active_grant(
    session: AsyncSession, user_id: uuid.UUID, repository_id: uuid.UUID
) -> bool:
    """True when the user holds a non-expired grant on the repository — the single
    active-grant predicate the query endpoint and source-content endpoint both gate on."""
    stmt = (
        sa.select(Grant.id)
        .where(
            Grant.user_id == user_id,
            Grant.repository_id == repository_id,
            sa.or_(Grant.expires_at.is_(None), Grant.expires_at > sa.func.now()),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def list_accessible_repositories(
    session: AsyncSession, user_id: uuid.UUID
) -> Sequence[Repository]:
    """Repositories the user holds an **active** (non-expired) grant on, by name.

    This is the user's repo picker: it excludes repositories they were never granted
    and grants whose ``expires_at`` has passed — the same active-grant rule enforced
    at query time.
    """
    result = await session.execute(
        sa.select(Repository)
        .join(Grant, Grant.repository_id == Repository.id)
        .where(
            Grant.user_id == user_id,
            sa.or_(Grant.expires_at.is_(None), Grant.expires_at > sa.func.now()),
        )
        .order_by(Repository.name)
    )
    return result.scalars().all()
