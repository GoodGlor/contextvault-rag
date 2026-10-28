"""The asset library: upload, list, fetch bytes, delete — scoped to a repository.

The scoping is the point. Grants in this app are per repository, so an asset
reachable across repositories would be a read of another tenant's bytes through
the render path.
"""

import io
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

import contextvault.api.report_assets as report_assets_api
from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import Grant, Repository, Role, User
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


def _png(width: int = 40, height: int = 20) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


async def _repo(session: AsyncSession, name: str = "Бухгалтерія") -> Repository:
    repo = Repository(name=name, description="test")
    session.add(repo)
    await session.flush()
    return repo


async def _grant(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    repo_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
) -> None:
    db_session.add(Grant(user_id=user_id, repository_id=repo_id, expires_at=expires_at))
    await db_session.flush()


@pytest.mark.asyncio
async def test_upload_then_list_then_fetch_bytes(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session)
    await db_session.commit()
    headers = _auth(await _token(client, "admin"))

    created = await client.post(
        f"/repositories/{repo.id}/report-assets",
        files={"file": ("logo.png", _png(320, 64), "image/png")},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["mime"] == "image/png"
    assert (body["width"], body["height"]) == (320, 64)
    assert "data" not in body  # the list model never carries bytes

    listed = await client.get(f"/repositories/{repo.id}/report-assets", headers=headers)
    assert [a["id"] for a in listed.json()] == [body["id"]]
    assert all("data" not in a for a in listed.json())

    raw = await client.get(f"/repositories/{repo.id}/report-assets/{body['id']}", headers=headers)
    assert raw.status_code == 200
    assert raw.headers["content-type"] == "image/png"
    assert raw.content[:4] == b"\x89PNG"


@pytest.mark.asyncio
async def test_an_svg_upload_is_refused(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session)
    await db_session.commit()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>'

    response = await client.post(
        f"/repositories/{repo.id}/report-assets",
        files={"file": ("logo.svg", svg, "image/svg+xml")},
        headers=_auth(await _token(client, "admin")),
    )
    assert response.status_code == 422
    assert "SVG" in response.json()["detail"]


@pytest.mark.asyncio
async def test_an_asset_is_not_reachable_through_another_repository(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """The whole reason assets are repository-scoped."""
    await _user(db_session, Role.ADMIN, "admin")
    owner = await _repo(db_session, "Бухгалтерія")
    other = await _repo(db_session, "HR")
    await db_session.commit()
    headers = _auth(await _token(client, "admin"))

    created = await client.post(
        f"/repositories/{owner.id}/report-assets",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=headers,
    )
    asset_id = created.json()["id"]

    leaked = await client.get(f"/repositories/{other.id}/report-assets/{asset_id}", headers=headers)
    assert leaked.status_code == 404


@pytest.mark.asyncio
async def test_delete_removes_it(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    repo = await _repo(db_session)
    await db_session.commit()
    headers = _auth(await _token(client, "admin"))
    created = await client.post(
        f"/repositories/{repo.id}/report-assets",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=headers,
    )
    asset_id = created.json()["id"]

    gone = await client.delete(f"/repositories/{repo.id}/report-assets/{asset_id}", headers=headers)
    assert gone.status_code == 204
    listed = await client.get(f"/repositories/{repo.id}/report-assets", headers=headers)
    assert listed.json() == []


# --------------------------------------------------------------------------- #
# Access control: the same guard as the query/report endpoints — a repository must exist
# and, for a non-admin, must be granted.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_asset_routes_403_without_a_grant(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """A non-admin with no grant on the repository is refused on every route."""
    await _user(db_session, Role.ADMIN, "admin")
    await _user(db_session, Role.USER, "stranger")
    repo = await _repo(db_session)
    await db_session.commit()

    admin_headers = _auth(await _token(client, "admin"))
    created = await client.post(
        f"/repositories/{repo.id}/report-assets",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=admin_headers,
    )
    asset_id = created.json()["id"]

    stranger_headers = _auth(await _token(client, "stranger"))

    upload = await client.post(
        f"/repositories/{repo.id}/report-assets",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=stranger_headers,
    )
    assert upload.status_code == 403

    listed = await client.get(f"/repositories/{repo.id}/report-assets", headers=stranger_headers)
    assert listed.status_code == 403

    fetched = await client.get(
        f"/repositories/{repo.id}/report-assets/{asset_id}", headers=stranger_headers
    )
    assert fetched.status_code == 403

    deleted = await client.delete(
        f"/repositories/{repo.id}/report-assets/{asset_id}", headers=stranger_headers
    )
    assert deleted.status_code == 403


@pytest.mark.asyncio
async def test_asset_routes_succeed_for_a_non_admin_with_an_active_grant(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    user = await _user(db_session, Role.USER, "alice")
    repo = await _repo(db_session)
    await _grant(db_session, user.id, repo.id)
    await db_session.commit()
    headers = _auth(await _token(client, "alice"))

    created = await client.post(
        f"/repositories/{repo.id}/report-assets",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]

    listed = await client.get(f"/repositories/{repo.id}/report-assets", headers=headers)
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()] == [asset_id]

    fetched = await client.get(f"/repositories/{repo.id}/report-assets/{asset_id}", headers=headers)
    assert fetched.status_code == 200

    deleted = await client.delete(
        f"/repositories/{repo.id}/report-assets/{asset_id}", headers=headers
    )
    assert deleted.status_code == 204


@pytest.mark.asyncio
async def test_asset_routes_404_for_an_unknown_repository(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.ADMIN, "admin")
    await db_session.commit()
    headers = _auth(await _token(client, "admin"))
    unknown_repo = uuid.uuid4()
    unknown_asset = uuid.uuid4()

    upload = await client.post(
        f"/repositories/{unknown_repo}/report-assets",
        files={"file": ("logo.png", _png(), "image/png")},
        headers=headers,
    )
    assert upload.status_code == 404

    listed = await client.get(f"/repositories/{unknown_repo}/report-assets", headers=headers)
    assert listed.status_code == 404

    fetched = await client.get(
        f"/repositories/{unknown_repo}/report-assets/{unknown_asset}", headers=headers
    )
    assert fetched.status_code == 404

    deleted = await client.delete(
        f"/repositories/{unknown_repo}/report-assets/{unknown_asset}", headers=headers
    )
    assert deleted.status_code == 404


# --------------------------------------------------------------------------- #
# The list query must not haul the image bytes over (fix round 1)
# --------------------------------------------------------------------------- #


def _selected(statement: Any) -> list[str]:
    """The statement's select list, one expression per item."""
    select_list = str(statement).split("\nFROM")[0].removeprefix("SELECT ")
    return [item.strip() for item in select_list.split(",")]


def test_the_list_query_never_fetches_the_bytes() -> None:
    """A response that never shows ``data`` is not proof it was never fetched —
    ``load_only`` has to keep it out of the select list itself."""
    columns = _selected(report_assets_api._list_statement(uuid.uuid4()))
    assert "report_assets.filename" in columns
    assert "report_assets.mime" in columns
    assert "report_assets.width" in columns
    assert "report_assets.height" in columns
    assert "report_assets.byte_size" in columns
    assert "report_assets.data" not in columns
