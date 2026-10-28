"""Tests for the user service (lookup + creation)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.security import verify_password
from contextvault.models import Role
from contextvault.services import users as user_service


async def test_create_user_hashes_password(db_session: AsyncSession) -> None:
    user = await user_service.create_user(
        db_session, username="alice", password="pw", role=Role.USER
    )
    assert user.password_hash != "pw"
    assert verify_password("pw", user.password_hash)
    assert user.role is Role.USER


async def test_get_user_by_username_roundtrips(db_session: AsyncSession) -> None:
    await user_service.create_user(db_session, username="bob", password="pw", role=Role.ADMIN)
    found = await user_service.get_user_by_username(db_session, "bob")
    assert found is not None and found.role is Role.ADMIN
    assert await user_service.get_user_by_username(db_session, "nobody") is None


async def test_get_user_by_id_roundtrips(db_session: AsyncSession) -> None:
    user = await user_service.create_user(
        db_session, username="carol", password="pw", role=Role.USER
    )
    assert (await user_service.get_user_by_id(db_session, user.id)) is not None
    assert (await user_service.get_user_by_id(db_session, uuid.uuid4())) is None


async def test_admin_exists_reflects_admins_only(db_session: AsyncSession) -> None:
    assert await user_service.admin_exists(db_session) is False
    await user_service.create_user(db_session, username="u", password="pw", role=Role.USER)
    assert await user_service.admin_exists(db_session) is False
    await user_service.create_user(db_session, username="a", password="pw", role=Role.ADMIN)
    assert await user_service.admin_exists(db_session) is True
