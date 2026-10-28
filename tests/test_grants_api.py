"""Integration tests for admin access grants + user-visible repositories.

Access model: users ↔ repositories is a many-to-many grant with an
optional expiry, and every retrieval is hard-filtered to the caller's *active*
grants. This card adds the admin management surface and the user-facing listing:

1. an admin can **grant** a user access to a repository, optionally time-boxed
   (``expires_at``), and **revoke** it;
2. granting is **idempotent** — re-granting the same pair updates the expiry rather
   than erroring on the unique constraint;
3. a user's ``GET /repositories`` shows **only** the repositories they hold an
   active (non-expired) grant on — never others', never expired ones;
4. grant/revoke are **admin-only**.

Query-time enforcement itself already exists (``retrieval.search`` and the query
endpoint filter on active grants); these tests cover the new management + listing.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Repository, Role, User
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


async def _repo(db_session: AsyncSession, name: str) -> Repository:
    repo = Repository(name=name)
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _token(client: AsyncClient, username: str) -> str:
    resp = await client.post("/auth/login", json={"username": username, "password": "pw"})
    return str(resp.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Grant
# --------------------------------------------------------------------------- #


async def test_admin_grants_access(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")

    resp = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id)},
        headers=_auth(admin),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == str(target.id)
    assert body["repository_id"] == str(repo.id)
    assert body["expires_at"] is None

    # The reader can now see the repo in their accessible list.
    listed = await client.get("/repositories", headers=_auth(await _token(client, "reader")))
    assert [r["id"] for r in listed.json()] == [str(repo.id)]


async def test_grant_with_future_expiry_is_active(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    resp = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id), "expires_at": future},
        headers=_auth(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] is not None

    listed = await client.get("/repositories", headers=_auth(await _token(client, "reader")))
    assert [r["id"] for r in listed.json()] == [str(repo.id)]


async def test_expired_grant_denies_visibility(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")
    # Clearly in the past (not a 1-second margin that could race the request clock).
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

    resp = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id), "expires_at": past},
        headers=_auth(admin),
    )
    assert resp.status_code == 200

    # An expired grant grants nothing: the repo is invisible to the reader.
    listed = await client.get("/repositories", headers=_auth(await _token(client, "reader")))
    assert listed.json() == []


async def test_grant_is_idempotent_and_updates_expiry(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Re-granting the same (user, repo) updates the expiry instead of failing the
    unique constraint — an admin 'grant access' is idempotent."""
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")

    first = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id)},
        headers=_auth(admin),
    )
    assert first.status_code == 200
    assert first.json()["expires_at"] is None

    future = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    second = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id), "expires_at": future},
        headers=_auth(admin),
    )
    assert second.status_code == 200
    assert second.json()["expires_at"] is not None
    # Same grant row (idempotent), not a duplicate.
    assert second.json()["id"] == first.json()["id"]


async def test_grant_unknown_repo_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    admin = await _token(client, "admin")
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/grants",
        json={"user_id": str(target.id)},
        headers=_auth(admin),
    )
    assert resp.status_code == 404


async def test_grant_unknown_user_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")
    resp = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(uuid.uuid4())},
        headers=_auth(admin),
    )
    assert resp.status_code == 404


async def test_grant_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "regular")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    resp = await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id)},
        headers=_auth(await _token(client, "regular")),
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Revoke
# --------------------------------------------------------------------------- #


async def test_admin_revokes_access(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")
    await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id)},
        headers=_auth(admin),
    )

    resp = await client.request(
        "DELETE", f"/repositories/{repo.id}/grants/{target.id}", headers=_auth(admin)
    )
    assert resp.status_code == 204

    listed = await client.get("/repositories", headers=_auth(await _token(client, "reader")))
    assert listed.json() == []


async def test_revoke_nonexistent_grant_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")
    resp = await client.request(
        "DELETE", f"/repositories/{repo.id}/grants/{target.id}", headers=_auth(admin)
    )
    assert resp.status_code == 404


async def test_revoke_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    regular = await _user(db_session, Role.USER, "regular")
    target = await _user(db_session, Role.USER, "reader")
    repo = await _repo(db_session, "Handbook")
    await client.post(
        f"/repositories/{repo.id}/grants",
        json={"user_id": str(target.id)},
        headers=_auth(await _token(client, "admin")),
    )
    resp = await client.request(
        "DELETE",
        f"/repositories/{repo.id}/grants/{target.id}",
        headers=_auth(await _token(client, "regular")),
    )
    assert resp.status_code == 403
    assert regular.id  # silence unused


# --------------------------------------------------------------------------- #
# List grants for a repo (admin)
# --------------------------------------------------------------------------- #


async def test_admin_lists_grants_for_repo(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    a = await _user(db_session, Role.USER, "alice")
    b = await _user(db_session, Role.USER, "bob")
    repo = await _repo(db_session, "Handbook")
    admin = await _token(client, "admin")
    for u in (a, b):
        await client.post(
            f"/repositories/{repo.id}/grants",
            json={"user_id": str(u.id)},
            headers=_auth(admin),
        )

    resp = await client.get(f"/repositories/{repo.id}/grants", headers=_auth(admin))
    assert resp.status_code == 200
    assert {g["user_id"] for g in resp.json()} == {str(a.id), str(b.id)}


async def test_list_grants_requires_admin(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "regular")
    repo = await _repo(db_session, "Handbook")
    resp = await client.get(
        f"/repositories/{repo.id}/grants", headers=_auth(await _token(client, "regular"))
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# GET /repositories — a user sees only their granted repos
# --------------------------------------------------------------------------- #


async def test_list_repositories_only_granted(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    reader = await _user(db_session, Role.USER, "reader")
    granted = await _repo(db_session, "Granted")
    await _repo(db_session, "Ungranted")
    admin = await _token(client, "admin")
    await client.post(
        f"/repositories/{granted.id}/grants",
        json={"user_id": str(reader.id)},
        headers=_auth(admin),
    )

    resp = await client.get("/repositories", headers=_auth(await _token(client, "reader")))
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()]
    assert names == ["Granted"]  # not "Ungranted"


async def test_list_repositories_requires_auth(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    resp = await client.get("/repositories")
    assert resp.status_code == 401
