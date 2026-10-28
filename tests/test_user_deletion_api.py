"""Integration tests for admin user deletion / anonymization.

Deletion anonymizes: an admin permanently removes a user, but
the act preserves analytics signal. Concretely, over the async ``db_session``:

1. deletion is **confirmation-gated** — the client must echo the target's username;
2. it **cascade-removes** the user's access grants;
3. it **detaches** the user's other contributions (e.g. admin-authored sources go
   ``created_by = NULL``) rather than deleting them — "by a deleted user";
4. it is **admin-only**, and it refuses to remove the **last remaining admin** so the
   system can never be locked out of its own bootstrap invariant.

The query-log leg of the spec ("detach past questions") is intentionally *not*
here: no query-log table exists yet. When #30 lands, its ``user_id`` FK
gets ``ON DELETE SET NULL`` and this same delete anonymizes those rows for free.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Grant, Repository, Role, Source, SourceKind, User
from contextvault.services import users as user_service


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _user(db_session: AsyncSession, role: Role, username: str) -> User:
    return await user_service.create_user(db_session, username=username, password="pw", role=role)


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _delete(username: str) -> dict[str, str]:
    return {"confirm_username": username}


# --------------------------------------------------------------------------- #
# Happy path — delete, cascade grants, detach contributions
# --------------------------------------------------------------------------- #


async def test_admin_deletes_user(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "victim")
    admin_token = await _token(client, "admin")

    resp = await client.request(
        "DELETE",
        f"/users/{target.id}",
        json=_delete("victim"),
        headers=_auth(admin_token),
    )
    assert resp.status_code == 204

    assert await user_service.get_user_by_id(db_session, target.id) is None


async def test_delete_cascades_grants(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "granted")
    repo = Repository(name="Handbook")
    db_session.add(repo)
    await db_session.flush()
    db_session.add(Grant(user_id=target.id, repository_id=repo.id))
    await db_session.flush()
    admin_token = await _token(client, "admin")

    resp = await client.request(
        "DELETE", f"/users/{target.id}", json=_delete("granted"), headers=_auth(admin_token)
    )
    assert resp.status_code == 204

    grants = await db_session.execute(sa.select(Grant).where(Grant.user_id == target.id))
    assert grants.first() is None
    # The repository itself survives — only the user's access to it is gone.
    assert await db_session.get(Repository, repo.id) is not None


async def test_delete_detaches_authored_sources(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A deleted user's admin notes survive with ``created_by`` nulled ("by a
    deleted user") — the anonymize-not-delete rule for existing contributions."""
    await _user(db_session, Role.ADMIN, "admin")
    author = await _user(db_session, Role.ADMIN, "author")
    repo = Repository(name="Notes")
    db_session.add(repo)
    await db_session.flush()
    note = Source(
        repository_id=repo.id, kind=SourceKind.ADMIN_NOTE, title="Policy", created_by=author.id
    )
    db_session.add(note)
    await db_session.flush()
    admin_token = await _token(client, "admin")

    resp = await client.request(
        "DELETE", f"/users/{author.id}", json=_delete("author"), headers=_auth(admin_token)
    )
    assert resp.status_code == 204

    await db_session.refresh(note)
    assert note.created_by is None  # detached, not deleted


# --------------------------------------------------------------------------- #
# Confirmation gate
# --------------------------------------------------------------------------- #


async def test_delete_requires_matching_confirmation(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "victim")
    admin_token = await _token(client, "admin")

    resp = await client.request(
        "DELETE", f"/users/{target.id}", json=_delete("wrong-name"), headers=_auth(admin_token)
    )
    assert resp.status_code == 400
    # The wrong confirmation is a no-op: the account is untouched.
    assert await user_service.get_user_by_id(db_session, target.id) is not None


async def test_delete_requires_confirmation_field(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "victim")
    admin_token = await _token(client, "admin")

    resp = await client.request(
        "DELETE", f"/users/{target.id}", json={}, headers=_auth(admin_token)
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


async def test_delete_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "regular")
    target = await _user(db_session, Role.USER, "other")
    token = await _token(client, "regular")
    resp = await client.request(
        "DELETE", f"/users/{target.id}", json=_delete("other"), headers=_auth(token)
    )
    assert resp.status_code == 403
    assert await user_service.get_user_by_id(db_session, target.id) is not None


async def test_delete_requires_authentication(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    target = await _user(db_session, Role.USER, "lonely")
    resp = await client.request("DELETE", f"/users/{target.id}", json=_delete("lonely"))
    assert resp.status_code == 401


async def test_delete_unknown_user_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    admin_token = await _token(client, "admin")
    resp = await client.request(
        "DELETE", f"/users/{uuid.uuid4()}", json=_delete("ghost"), headers=_auth(admin_token)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Last-admin guard — never lock the system out of its bootstrap invariant
# --------------------------------------------------------------------------- #


async def test_cannot_delete_last_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    admin = await _user(db_session, Role.ADMIN, "admin")
    admin_token = await _token(client, "admin")
    resp = await client.request(
        "DELETE", f"/users/{admin.id}", json=_delete("admin"), headers=_auth(admin_token)
    )
    assert resp.status_code == 409
    assert await user_service.get_user_by_id(db_session, admin.id) is not None


async def test_can_delete_admin_when_another_remains(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    other_admin = await _user(db_session, Role.ADMIN, "second")
    admin_token = await _token(client, "admin")
    resp = await client.request(
        "DELETE", f"/users/{other_admin.id}", json=_delete("second"), headers=_auth(admin_token)
    )
    assert resp.status_code == 204
    assert await user_service.get_user_by_id(db_session, other_admin.id) is None
