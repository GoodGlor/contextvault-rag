"""Command-line entrypoint.

Run with ``python -m contextvault.cli <command>``. Currently provides
``create-admin`` for the first-admin bootstrap.
"""

import argparse
import asyncio
from typing import Protocol

from contextvault.core.config import get_settings
from contextvault.db.session import SessionLocal
from contextvault.services.bootstrap import create_first_admin


class _Args(Protocol):
    username: str | None
    password: str | None


class _AdminDefaults(Protocol):
    initial_admin_username: str | None
    initial_admin_password: str | None


def resolve_admin_credentials(args: _Args, settings: _AdminDefaults) -> tuple[str, str]:
    """Take credentials from CLI flags, falling back to settings/env.

    Exits with an error if neither source supplies both a username and password.
    """
    username = args.username or settings.initial_admin_username
    password = args.password or settings.initial_admin_password
    if not username or not password:
        raise SystemExit(
            "username and password are required: pass --username/--password or set "
            "INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD"
        )
    return username, password


async def _create_admin(username: str, password: str) -> str:
    async with SessionLocal() as session, session.begin():
        admin = await create_first_admin(session, username=username, password=password)
    if admin is None:
        return "An admin already exists; nothing to do."
    return f"Created admin '{username}'."


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="contextvault")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_admin = subparsers.add_parser(
        "create-admin", help="Create the first admin account if none exists"
    )
    create_admin.add_argument("--username", help="Admin username")
    create_admin.add_argument("--password", help="Admin password")

    args = parser.parse_args(argv)
    if args.command == "create-admin":
        username, password = resolve_admin_credentials(args, get_settings())
        print(asyncio.run(_create_admin(username, password)))


if __name__ == "__main__":
    main()
