"""First-admin bootstrap.

Creating the very first admin is a chicken-and-egg problem: there is no admin
yet to invite one. ``create_first_admin`` seeds one, but only while no admin
exists, so it is safe to run repeatedly (e.g. on every deploy).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import Role, User
from contextvault.services import users as user_service


async def create_first_admin(session: AsyncSession, *, username: str, password: str) -> User | None:
    """Create an admin if none exists yet; otherwise do nothing.

    Returns the newly created admin, or ``None`` when an admin already existed.
    """
    if await user_service.admin_exists(session):
        return None
    return await user_service.create_user(
        session, username=username, password=password, role=Role.ADMIN
    )
