"""Tests for first-admin bootstrap."""

from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import Role
from contextvault.services import users as user_service
from contextvault.services.bootstrap import create_first_admin


async def test_creates_admin_when_none_exists(db_session: AsyncSession) -> None:
    user = await create_first_admin(db_session, username="root", password="pw")
    assert user is not None
    assert user.role is Role.ADMIN
    assert (await user_service.get_user_by_username(db_session, "root")) is not None


async def test_is_idempotent_when_admin_exists(db_session: AsyncSession) -> None:
    first = await create_first_admin(db_session, username="root", password="pw")
    assert first is not None
    # A second bootstrap is a no-op — it must not create a second admin.
    second = await create_first_admin(db_session, username="other", password="pw")
    assert second is None
    assert (await user_service.get_user_by_username(db_session, "other")) is None


async def test_bootstrap_admin_can_authenticate(db_session: AsyncSession) -> None:
    from contextvault.core.security import verify_password

    user = await create_first_admin(db_session, username="root", password="s3cret")
    assert user is not None
    assert verify_password("s3cret", user.password_hash)
