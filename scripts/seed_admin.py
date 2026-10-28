"""Ensure an admin account exists with a known password.

Extracted from ``dev.sh`` when CI grew an end-to-end job (#214) that needs the
same account: the e2e specs sign in as this user, so a password that drifted
between the local stack and CI would fail every spec at the sign-in step and
look like a broken suite rather than a broken fixture.

Reads ``ADMIN_USER`` / ``ADMIN_PASS`` from the environment, defaulting to the
same values ``dev.sh`` documents at the top of the file. Never for production:
it resets an existing account's password on purpose, so that logging in always
works in development.
"""

import asyncio
import os

from contextvault.db.session import SessionLocal
from contextvault.models import Role
from contextvault.services import users as user_service


async def main() -> None:
    username = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASS", "adminpass123")
    async with SessionLocal() as session, session.begin():
        user = await user_service.get_user_by_username(session, username)
        if user is None:
            await user_service.create_user(
                session, username=username, password=password, role=Role.ADMIN
            )
            print(f"  Created admin '{username}'.")
        else:
            # Reset to the known password so login always works in dev and CI.
            await user_service.change_password(session, user, new_password=password)
            print(f"  Reset password for existing admin '{username}'.")


if __name__ == "__main__":
    asyncio.run(main())
