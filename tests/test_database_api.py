"""Admin database-connection API (Task 9, DB-reports spec).

An admin points a repository at a read-only external SQL database: ``PUT``
live-tests the connection before storing it, the password is never returned by
any route, the exposed-schema allow-list can be edited without re-testing, and
``introspect`` reads live tables/columns from the *stored* connection.

Liveness (``test_connection`` / ``introspect_schema``) is monkeypatched by
attribute string — never imported by name — so the suite never dials a real
external database and pytest never mistakes the ``test_``-named import for a
test function.
"""

import datetime
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.llm.textgen import TextGenError
from contextvault.main import create_app
from contextvault.models import LLMProviderName, ProviderSetting, Repository, Role, User
from contextvault.services import users as user_service
from contextvault.services.report_db import DBConnectionError


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


async def _repo(db_session: AsyncSession) -> Repository:
    repo = Repository(name="Reporting Vault")
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _repo_with_llm(db_session: AsyncSession) -> Repository:
    """A repository that can actually call a model — what #202's describe needs.

    ``repo_is_answerable`` wants a picked model *and* a verified key for its
    provider, so both halves are set up here; a repo with only the model chosen
    is the "not answerable" case and has its own test.
    """
    db_session.add(
        ProviderSetting(
            provider=LLMProviderName.OPENAI,
            api_key_encrypted=encrypt("sk-test"),
            verified_at=datetime.datetime.now(datetime.UTC),
        )
    )
    repo = Repository(
        name="Reporting Vault", llm_provider=LLMProviderName.OPENAI, llm_model="gpt-4o"
    )
    db_session.add(repo)
    await db_session.flush()
    return repo


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": "Reporting",
        "db_type": "postgres",
        "host": "reporting.internal",
        "port": 5432,
        "database": "sales",
        "username": "reporter",
        "password": "s3cret",
    }
    body.update(overrides)
    return body


def _assert_no_password_leak(resp_json: object) -> None:
    assert "password" not in str(resp_json)
    assert "s3cret" not in str(resp_json)


# --------------------------------------------------------------------------- #
# PUT — live-test, encrypt, mask, upsert
# --------------------------------------------------------------------------- #


async def test_put_tests_connection_encrypts_and_masks(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_test_connection(**kwargs: object) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin1")
    token = await _token(client, "admin1")

    resp = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(),
        headers=_auth(token),
    )
    assert resp.status_code == 201
    body = resp.json()
    _assert_no_password_leak(body)
    assert body["name"] == "Reporting"
    assert body["host"] == "reporting.internal"
    assert body["db_type"] == "postgres"
    assert body["username"] == "reporter"
    first_id = body["id"]
    assert len(calls) == 1
    assert calls[0]["password"] == "s3cret"

    got = await client.get(f"/repositories/{repo.id}/databases/{first_id}", headers=_auth(token))
    assert got.status_code == 200
    got_body = got.json()
    _assert_no_password_leak(got_body)
    assert got_body["username"] == "reporter"

    # PUT on the id edits that row in place — same id, new host, no second row.
    resp2 = await client.put(
        f"/repositories/{repo.id}/databases/{first_id}",
        json=_payload(host="reporting2.internal"),
        headers=_auth(token),
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["id"] == first_id
    assert body2["host"] == "reporting2.internal"

    listed = await client.get(f"/repositories/{repo.id}/databases", headers=_auth(token))
    assert [c["id"] for c in listed.json()] == [first_id]


async def test_put_update_keeps_password_when_omitted(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_passwords: list[object] = []

    async def fake_test_connection(**kwargs: object) -> None:
        seen_passwords.append(kwargs["password"])

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin2")
    token = await _token(client, "admin2")

    first = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert first.status_code == 201
    connection_id = first.json()["id"]

    # Update without a password (empty string) — must keep the stored one, so
    # the live re-test is called with the original secret, not "".
    second = await client.put(
        f"/repositories/{repo.id}/databases/{connection_id}",
        json=_payload(host="new-host.internal", password=""),
        headers=_auth(token),
    )
    assert second.status_code == 200
    assert seen_passwords == ["s3cret", "s3cret"]
    _assert_no_password_leak(second.json())


async def test_put_update_keeps_exposed_schema_when_omitted(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin_schema_keep")
    token = await _token(client, "admin_schema_keep")

    create_resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert create_resp.status_code == 201
    connection_id = create_resp.json()["id"]

    allow_list = [
        {
            "table": "orders",
            "description": "Customer orders",
            "columns": [{"name": "id", "description": "Primary key"}],
        }
    ]
    patch_resp = await client.patch(
        f"/repositories/{repo.id}/databases/{connection_id}/schema",
        json={"exposed_schema": allow_list},
        headers=_auth(token),
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["exposed_schema"] == allow_list

    # A later PUT fixes the host but omits exposed_schema entirely — the
    # curated allow-list set via PATCH must survive, not be wiped to [].
    put_resp2 = await client.put(
        f"/repositories/{repo.id}/databases/{connection_id}",
        json=_payload(host="reporting3.internal"),
        headers=_auth(token),
    )
    assert put_resp2.status_code == 200
    assert put_resp2.json()["exposed_schema"] == allow_list

    got = await client.get(
        f"/repositories/{repo.id}/databases/{connection_id}", headers=_auth(token)
    )
    assert got.status_code == 200
    assert got.json()["exposed_schema"] == allow_list


async def test_put_rejects_unreachable_database(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        raise DBConnectionError("no route to host")

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin3")
    token = await _token(client, "admin3")

    resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "no route to host" in resp.json()["detail"]

    # Nothing was stored — the repository still holds no connections.
    listed = await client.get(f"/repositories/{repo.id}/databases", headers=_auth(token))
    assert listed.status_code == 200
    assert listed.json() == []


# --------------------------------------------------------------------------- #
# Authorization / 404s
# --------------------------------------------------------------------------- #


async def test_requires_admin(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        return None

    async def fake_introspect_schema(**kwargs: object) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.api.database.introspect_schema", fake_introspect_schema)

    repo = await _repo(db_session)
    await _user(db_session, Role.USER, "regular")
    token = await _token(client, "regular")

    # A non-admin is refused before any lookup, so the id need not exist — and
    # a 403 rather than a 404 is the point: authorization decides first.
    some_id = uuid.uuid4()

    create_resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert create_resp.status_code == 403

    list_resp = await client.get(f"/repositories/{repo.id}/databases", headers=_auth(token))
    assert list_resp.status_code == 403

    get_resp = await client.get(
        f"/repositories/{repo.id}/databases/{some_id}", headers=_auth(token)
    )
    assert get_resp.status_code == 403

    put_resp = await client.put(
        f"/repositories/{repo.id}/databases/{some_id}",
        json=_payload(),
        headers=_auth(token),
    )
    assert put_resp.status_code == 403

    patch_resp = await client.patch(
        f"/repositories/{repo.id}/databases/{some_id}/schema",
        json={"exposed_schema": []},
        headers=_auth(token),
    )
    assert patch_resp.status_code == 403

    delete_resp = await client.delete(
        f"/repositories/{repo.id}/databases/{some_id}", headers=_auth(token)
    )
    assert delete_resp.status_code == 403

    introspect_resp = await client.post(
        f"/repositories/{repo.id}/databases/{some_id}/introspect", headers=_auth(token)
    )
    assert introspect_resp.status_code == 403


async def test_unknown_repo_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.ADMIN, "admin4")
    token = await _token(client, "admin4")
    ghost = uuid.uuid4()
    some_id = uuid.uuid4()

    create_resp = await client.post(
        f"/repositories/{ghost}/databases", json=_payload(), headers=_auth(token)
    )
    assert create_resp.status_code == 404

    list_resp = await client.get(f"/repositories/{ghost}/databases", headers=_auth(token))
    assert list_resp.status_code == 404

    get_resp = await client.get(f"/repositories/{ghost}/databases/{some_id}", headers=_auth(token))
    assert get_resp.status_code == 404

    put_resp = await client.put(
        f"/repositories/{ghost}/databases/{some_id}", json=_payload(), headers=_auth(token)
    )
    assert put_resp.status_code == 404

    patch_resp = await client.patch(
        f"/repositories/{ghost}/databases/{some_id}/schema",
        json={"exposed_schema": []},
        headers=_auth(token),
    )
    assert patch_resp.status_code == 404

    delete_resp = await client.delete(
        f"/repositories/{ghost}/databases/{some_id}", headers=_auth(token)
    )
    assert delete_resp.status_code == 404

    introspect_resp = await client.post(
        f"/repositories/{ghost}/databases/{some_id}/introspect", headers=_auth(token)
    )
    assert introspect_resp.status_code == 404


# --------------------------------------------------------------------------- #
# PATCH schema — allow-list edits, no re-test
# --------------------------------------------------------------------------- #


async def test_patch_schema_saves_allow_list(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin5")
    token = await _token(client, "admin5")

    create_resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert create_resp.status_code == 201
    connection_id = create_resp.json()["id"]

    allow_list = [
        {
            "table": "orders",
            "description": "Customer orders",
            "columns": [
                {"name": "id", "description": "Primary key"},
                {"name": "total", "description": "Order total in cents"},
            ],
        }
    ]
    patch_resp = await client.patch(
        f"/repositories/{repo.id}/databases/{connection_id}/schema",
        json={"exposed_schema": allow_list},
        headers=_auth(token),
    )
    assert patch_resp.status_code == 200
    patch_body = patch_resp.json()
    _assert_no_password_leak(patch_body)
    assert patch_body["exposed_schema"] == allow_list

    got = await client.get(
        f"/repositories/{repo.id}/databases/{connection_id}", headers=_auth(token)
    )
    assert got.status_code == 200
    got_body = got.json()
    _assert_no_password_leak(got_body)
    assert got_body["exposed_schema"] == allow_list


# --------------------------------------------------------------------------- #
# Introspect — uses the stored connection
# --------------------------------------------------------------------------- #


async def test_introspect_uses_stored_connection(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        return None

    fixed_schema: list[dict[str, object]] = [
        {"table": "orders", "description": "", "columns": [{"name": "id", "description": ""}]}
    ]

    seen_kwargs: list[dict[str, object]] = []

    async def fake_introspect_schema(**kwargs: object) -> list[dict[str, object]]:
        seen_kwargs.append(kwargs)
        return fixed_schema

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.api.database.introspect_schema", fake_introspect_schema)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin6")
    token = await _token(client, "admin6")

    # A connection id that is not this repository's: 404. The singular route
    # answered 400 ("nothing is configured"), which was right when a repository
    # had at most one connection and the route named no particular one. The
    # route now names a specific connection, so this is a lookup failure.
    no_conn_resp = await client.post(
        f"/repositories/{repo.id}/databases/{uuid.uuid4()}/introspect", headers=_auth(token)
    )
    assert no_conn_resp.status_code == 404

    create_resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert create_resp.status_code == 201
    connection_id = create_resp.json()["id"]

    resp = await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/introspect", headers=_auth(token)
    )
    assert resp.status_code == 200
    resp_body = resp.json()
    _assert_no_password_leak(resp_body)
    assert resp_body == {"schema": fixed_schema}
    assert seen_kwargs[0]["password"] == "s3cret"
    assert seen_kwargs[0]["host"] == "reporting.internal"


async def test_introspect_rejects_unreachable(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        return None

    async def fake_introspect_schema(**kwargs: object) -> list[dict[str, object]]:
        raise DBConnectionError("connection refused")

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.api.database.introspect_schema", fake_introspect_schema)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin7")
    token = await _token(client, "admin7")

    created = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    connection_id = created.json()["id"]
    resp = await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/introspect", headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "connection refused" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# DELETE
# --------------------------------------------------------------------------- #


async def test_delete_removes_connection(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin8")
    token = await _token(client, "admin8")

    keep = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Keep me"),
        headers=_auth(token),
    )
    doomed = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Delete me", database="other"),
        headers=_auth(token),
    )
    keep_id, doomed_id = keep.json()["id"], doomed.json()["id"]

    delete_resp = await client.delete(
        f"/repositories/{repo.id}/databases/{doomed_id}", headers=_auth(token)
    )
    assert delete_resp.status_code == 204

    get_resp = await client.get(
        f"/repositories/{repo.id}/databases/{doomed_id}", headers=_auth(token)
    )
    assert get_resp.status_code == 404

    # Deleting one leaves the repository's others alone — the whole point of
    # holding several.
    listed = await client.get(f"/repositories/{repo.id}/databases", headers=_auth(token))
    assert [c["id"] for c in listed.json()] == [keep_id]


async def test_delete_unknown_connection_404(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin9")
    token = await _token(client, "admin9")

    resp = await client.delete(
        f"/repositories/{repo.id}/databases/{uuid.uuid4()}", headers=_auth(token)
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def test_put_rejects_invalid_port(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin10")
    token = await _token(client, "admin10")

    resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(port=0), headers=_auth(token)
    )
    assert resp.status_code == 422

    resp2 = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(port=70000), headers=_auth(token)
    )
    assert resp2.status_code == 422


async def test_put_rejects_empty_host(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin11")
    token = await _token(client, "admin11")

    resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(host=""), headers=_auth(token)
    )
    assert resp.status_code == 422


async def test_create_rejects_an_empty_name(db_session: AsyncSession, client: AsyncClient) -> None:
    """A blank name would make the picker #201 exists for unusable, and would
    collide with the next blank one under the new uniqueness rule."""
    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin12")
    token = await _token(client, "admin12")

    resp = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(name=""), headers=_auth(token)
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# #201 — several databases per repository
# --------------------------------------------------------------------------- #


async def test_a_repository_holds_several_connections(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of #201. The model enforced one row per repository
    (``uq_database_connections_repository_id``, "slice 1"), so a second
    connection was not a 409 — it was unrepresentable."""

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin_multi")
    token = await _token(client, "admin_multi")

    first = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Apple financials", database="apple"),
        headers=_auth(token),
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Microsoft financials", database="microsoft"),
        headers=_auth(token),
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"]

    listed = await client.get(f"/repositories/{repo.id}/databases", headers=_auth(token))
    assert listed.status_code == 200
    body = listed.json()
    _assert_no_password_leak(body)
    assert {c["name"] for c in body} == {"Apple financials", "Microsoft financials"}
    assert {c["database"] for c in body} == {"apple", "microsoft"}


async def test_two_connections_in_one_repository_cannot_share_a_name(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name is how an admin tells them apart in a picker, so a duplicate is
    refused with a reason rather than surfacing as an IntegrityError 500."""

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin_dup")
    token = await _token(client, "admin_dup")

    ok = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Financials"),
        headers=_auth(token),
    )
    assert ok.status_code == 201

    clash = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Financials", database="other"),
        headers=_auth(token),
    )
    assert clash.status_code == 409, clash.text
    assert "name" in clash.json()["detail"].lower()


async def test_the_same_name_is_free_in_a_different_repository(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The constraint is per repository, not global — two teams may each have a
    connection they both call "Financials"."""

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    one = await _repo(db_session)
    other = Repository(name="Second Vault")
    db_session.add(other)
    await db_session.flush()
    await _user(db_session, Role.ADMIN, "admin_two_repos")
    token = await _token(client, "admin_two_repos")

    for repo in (one, other):
        resp = await client.post(
            f"/repositories/{repo.id}/databases",
            json=_payload(name="Financials"),
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text


async def test_a_connection_cannot_be_read_through_another_repositorys_path(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nesting the id under a repository makes the pair the identity.

    Looking the connection up by id alone would let any admin path reach any
    repository's connection — the id is the only thing the caller supplies, and
    it is not secret.
    """

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    owner = await _repo(db_session)
    stranger = Repository(name="Unrelated Vault")
    db_session.add(stranger)
    await db_session.flush()
    await _user(db_session, Role.ADMIN, "admin_scope")
    token = await _token(client, "admin_scope")

    created = await client.post(
        f"/repositories/{owner.id}/databases",
        json=_payload(name="Financials"),
        headers=_auth(token),
    )
    assert created.status_code == 201
    connection_id = created.json()["id"]

    for method, suffix in (
        ("get", ""),
        ("delete", ""),
        ("patch", "/schema"),
        ("post", "/introspect"),
    ):
        resp = await getattr(client, method)(
            f"/repositories/{stranger.id}/databases/{connection_id}{suffix}",
            headers=_auth(token),
            **({"json": {"exposed_schema": []}} if method == "patch" else {}),
        )
        assert resp.status_code == 404, f"{method} {suffix} leaked: {resp.status_code}"

    # …and it is still reachable through its own repository.
    mine = await client.get(
        f"/repositories/{owner.id}/databases/{connection_id}", headers=_auth(token)
    )
    assert mine.status_code == 200


async def test_renaming_a_connection_onto_a_sibling_is_refused(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 409 has two call sites, and update is the one easy to leave out.

    Creating a duplicate is the obvious case; renaming an existing connection
    onto a sibling's name reaches the same constraint by a different route, and
    an uncaught IntegrityError there is a 500 rather than a reason.
    """

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin_rename")
    token = await _token(client, "admin_rename")

    first = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Apple"),
        headers=_auth(token),
    )
    second = await client.post(
        f"/repositories/{repo.id}/databases",
        json=_payload(name="Microsoft", database="msft"),
        headers=_auth(token),
    )
    assert first.status_code == second.status_code == 201

    clash = await client.put(
        f"/repositories/{repo.id}/databases/{second.json()['id']}",
        json=_payload(name="Apple", database="msft"),
        headers=_auth(token),
    )
    assert clash.status_code == 409, clash.text
    assert "name" in clash.json()["detail"].lower()

    # The rejected rename left both rows as they were.
    listed = await client.get(f"/repositories/{repo.id}/databases", headers=_auth(token))
    assert [c["name"] for c in listed.json()] == ["Apple", "Microsoft"]


async def test_a_connection_may_keep_its_own_name_on_update(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplicate check has to exclude the row being updated.

    Without ``exclude_id`` every edit that leaves the name alone — fixing a
    typo'd host, rotating the password — would collide with itself and 409.
    """

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin_self_name")
    token = await _token(client, "admin_self_name")

    created = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(name="Apple"), headers=_auth(token)
    )
    assert created.status_code == 201

    same_name = await client.put(
        f"/repositories/{repo.id}/databases/{created.json()['id']}",
        json=_payload(name="Apple", host="moved.internal"),
        headers=_auth(token),
    )
    assert same_name.status_code == 200, same_name.text
    assert same_name.json()["host"] == "moved.internal"


# --------------------------------------------------------------------------- #
# #202 — a connection describes itself
# --------------------------------------------------------------------------- #


async def _connect_with_schema(
    client: AsyncClient, repo_id: object, token: str, name: str = "Reporting"
) -> str:
    """Create a connection and give it an allow-list, which is what gets described."""
    created = await client.post(
        f"/repositories/{repo_id}/databases", json=_payload(name=name), headers=_auth(token)
    )
    assert created.status_code == 201, created.text
    connection_id = str(created.json()["id"])
    patched = await client.patch(
        f"/repositories/{repo_id}/databases/{connection_id}/schema",
        json={
            "exposed_schema": [
                {
                    "table": "orders",
                    "description": "Customer orders",
                    "columns": [{"name": "total", "description": "Order total"}],
                }
            ]
        },
        headers=_auth(token),
    )
    assert patched.status_code == 200
    return connection_id


async def test_describe_generates_and_stores_a_description(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen_prompts: list[str] = []

    async def fake_generate_text(*args: object, prompt: str = "", **kwargs: object) -> str:
        seen_prompts.append(prompt)
        return '  "Customer order totals."  '

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_describe")
    token = await _token(client, "admin_describe")
    connection_id = await _connect_with_schema(client, repo.id, token)

    resp = await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Normalised on the way in, not at every reader.
    assert body["description"] == "Customer order totals."
    assert body["description_stale"] is False
    # It described the ALLOW-LIST, not the live database.
    assert "Customer orders" in seen_prompts[0]

    got = await client.get(
        f"/repositories/{repo.id}/databases/{connection_id}", headers=_auth(token)
    )
    assert got.json()["description"] == "Customer order totals."


async def test_a_description_goes_stale_when_the_allow_list_changes(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fingerprint is the point of #202's staleness rule.

    A description that silently outlives the allow-list it describes would
    misroute a report in #203 with no sign anything was wrong.
    """

    async def fake_generate_text(*args: object, **kwargs: object) -> str:
        return "Customer order totals."

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_stale")
    token = await _token(client, "admin_stale")
    connection_id = await _connect_with_schema(client, repo.id, token)

    described = await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
    )
    assert described.json()["description_stale"] is False

    changed = await client.patch(
        f"/repositories/{repo.id}/databases/{connection_id}/schema",
        json={"exposed_schema": [{"table": "refunds", "description": "", "columns": []}]},
        headers=_auth(token),
    )
    assert changed.status_code == 200
    assert changed.json()["description_stale"] is True
    # The description itself is kept — a stale one still beats none while the
    # admin decides whether to regenerate.
    assert changed.json()["description"] == "Customer order totals."


async def test_a_connection_with_no_description_is_not_stale(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent is not out-of-date. Reporting a never-described connection as stale
    would put a warning on every new connection before anyone had done anything
    wrong."""

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_nodesc")
    token = await _token(client, "admin_nodesc")
    connection_id = await _connect_with_schema(client, repo.id, token)

    got = await client.get(
        f"/repositories/{repo.id}/databases/{connection_id}", headers=_auth(token)
    )
    assert got.json()["description"] == ""
    assert got.json()["description_stale"] is False


async def test_a_failed_generation_leaves_the_connection_usable(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling 3: failure never blocks. The connection is fully usable without a
    description, so a provider being down must not damage anything."""

    async def fake_generate_text(*args: object, **kwargs: object) -> str:
        raise TextGenError("quota exhausted")

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_fail")
    token = await _token(client, "admin_fail")
    connection_id = await _connect_with_schema(client, repo.id, token)

    resp = await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "quota exhausted" in resp.json()["detail"]

    # Still there, still curated, still no description.
    got = await client.get(
        f"/repositories/{repo.id}/databases/{connection_id}", headers=_auth(token)
    )
    assert got.status_code == 200
    assert got.json()["description"] == ""
    assert got.json()["exposed_schema"][0]["table"] == "orders"


async def test_a_description_can_be_corrected_by_hand(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-negotiable: in #203 a wrong description misroutes a report, so an
    admin must be able to fix one without re-testing the connection."""

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_edit_desc")
    token = await _token(client, "admin_edit_desc")
    connection_id = await _connect_with_schema(client, repo.id, token)

    resp = await client.patch(
        f"/repositories/{repo.id}/databases/{connection_id}/description",
        json={"description": "Apple Inc. financials, FY2016-2025."},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "Apple Inc. financials, FY2016-2025."
    # A hand-written description describes the allow-list as it stands now.
    assert resp.json()["description_stale"] is False


async def test_describe_refuses_before_there_is_an_allow_list(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection is created with an empty allow-list and curated afterwards.

    Describing one then would spend a model call to describe nothing, and store
    a confident sentence about a database the planner cannot query a column of.
    """
    called = False

    async def fake_generate_text(*args: object, **kwargs: object) -> str:
        nonlocal called
        called = True
        return "should never be reached"

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_noschema")
    token = await _token(client, "admin_noschema")
    created = await client.post(
        f"/repositories/{repo.id}/databases", json=_payload(), headers=_auth(token)
    )
    assert created.status_code == 201

    resp = await client.post(
        f"/repositories/{repo.id}/databases/{created.json()['id']}/describe",
        headers=_auth(token),
    )
    assert resp.status_code == 400
    assert "allow-list" in resp.json()["detail"]
    assert called is False, "the model was called to describe an empty allow-list"


async def test_describe_refuses_when_the_repository_has_no_usable_model(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_repo`` has no LLM configured, which is the ordinary state of a
    repository that only serves documents. The message has to name the fix."""

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

    repo = await _repo(db_session)
    await _user(db_session, Role.ADMIN, "admin_nomodel")
    token = await _token(client, "admin_nomodel")
    connection_id = await _connect_with_schema(client, repo.id, token)

    resp = await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "verified key" in resp.json()["detail"]


async def test_clearing_a_description_by_hand_also_clears_its_fingerprint(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blank is allowed — deleting a wrong description beats keeping it.

    The fingerprint has to go with it, or the connection would hold a hash of an
    allow-list no description was written from, and a later describe would have
    to know to ignore it.
    """

    async def fake_generate_text(*args: object, **kwargs: object) -> str:
        return "Customer order totals."

    async def fake_test_connection(**kwargs: object) -> None:
        return None

    monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
    monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

    repo = await _repo_with_llm(db_session)
    await _user(db_session, Role.ADMIN, "admin_clear")
    token = await _token(client, "admin_clear")
    connection_id = await _connect_with_schema(client, repo.id, token)
    await client.post(
        f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
    )

    cleared = await client.patch(
        f"/repositories/{repo.id}/databases/{connection_id}/description",
        json={"description": "   "},
        headers=_auth(token),
    )
    assert cleared.status_code == 200
    assert cleared.json()["description"] == ""
    assert cleared.json()["description_stale"] is False


# --------------------------------------------------------------------------- #
# #212 — the description writes itself
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_background_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the description task out of every API test in this module.

    These tests assert what the *endpoint* does. The task runs detached with its
    own ``SessionLocal``, so letting it execute inside a test's event loop leaks
    an asyncpg connection into the next test — the failure reads as
    ``Future ... attached to a different loop`` several tests later, nowhere near
    the cause. The report API tests patch their background entrypoints for the
    same reason; the task's own behaviour is covered directly in
    ``tests/test_connection_description.py``.

    Tests that care *whether* it was queued re-patch it with a recorder, which
    wins because it is applied after this fixture.
    """
    monkeypatch.setattr(
        "contextvault.api.database.run_connection_description",
        lambda *args, **kwargs: None,
    )


class TestTheDescriptionWritesItself:
    """The owner's requirement: nobody should have to press "Describe".

    #202 kept generation explicit and deliberately out of the allow-list save,
    because generating *inside* it would make every edit wait on a provider and
    fail when one is down. Both halves of that still hold here — the save
    commits and answers first, and a background task writes the description
    afterwards.
    """

    async def test_saving_an_allow_list_queues_a_description(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queued: list[tuple[object, ...]] = []

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
        monkeypatch.setattr(
            "contextvault.api.database.run_connection_description",
            lambda *args: queued.append(args),
        )

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_auto_1")
        token = await _token(client, "admin_auto_1")
        connection_id = await _connect_with_schema(client, repo.id, token)

        assert len(queued) == 1
        assert str(queued[0][0]) == connection_id

    async def test_re_saving_the_same_allow_list_queues_nothing(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The condition is the fingerprint, so curation that changed nothing
        does not spend a provider call."""
        queued: list[tuple[object, ...]] = []

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        async def fake_generate_text(*args: object, prompt: str = "", **kwargs: object) -> str:
            return "Customer order totals."

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
        monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_auto_2")
        token = await _token(client, "admin_auto_2")
        connection_id = await _connect_with_schema(client, repo.id, token)
        # Describe it for real, so the row now carries this allow-list's fingerprint.
        await client.post(
            f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
        )

        monkeypatch.setattr(
            "contextvault.api.database.run_connection_description",
            lambda *args: queued.append(args),
        )
        again = await client.patch(
            f"/repositories/{repo.id}/databases/{connection_id}/schema",
            json={
                "exposed_schema": [
                    {
                        "table": "orders",
                        "description": "Customer orders",
                        "columns": [{"name": "total", "description": "Order total"}],
                    }
                ]
            },
            headers=_auth(token),
        )
        assert again.status_code == 200
        assert queued == []

    async def test_changing_the_allow_list_queues_a_fresh_description(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner's case: a table added or a column removed rewrites it."""
        queued: list[tuple[object, ...]] = []

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        async def fake_generate_text(*args: object, prompt: str = "", **kwargs: object) -> str:
            return "Customer order totals."

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
        monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_auto_3")
        token = await _token(client, "admin_auto_3")
        connection_id = await _connect_with_schema(client, repo.id, token)
        await client.post(
            f"/repositories/{repo.id}/databases/{connection_id}/describe", headers=_auth(token)
        )

        monkeypatch.setattr(
            "contextvault.api.database.run_connection_description",
            lambda *args: queued.append(args),
        )
        changed = await client.patch(
            f"/repositories/{repo.id}/databases/{connection_id}/schema",
            json={
                "exposed_schema": [
                    {
                        "table": "orders",
                        "description": "Customer orders",
                        "columns": [
                            {"name": "total", "description": "Order total"},
                            {"name": "city", "description": "Billing city"},
                        ],
                    }
                ]
            },
            headers=_auth(token),
        )
        assert changed.status_code == 200
        assert len(queued) == 1

    async def test_the_save_still_succeeds_with_no_provider_reachable(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#202's objection, kept true: the allow-list save must not depend on a
        provider. The dispatch happens after the response is composed."""

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        async def exploding_generate_text(*args: object, **kwargs: object) -> str:
            raise AssertionError("the save must not call a provider")

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
        monkeypatch.setattr(
            "contextvault.services.report_llm.generate_text", exploding_generate_text
        )

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_auto_4")
        token = await _token(client, "admin_auto_4")
        # _connect_with_schema asserts the PATCH returned 200 — that IS the claim.
        connection_id = await _connect_with_schema(client, repo.id, token)
        assert connection_id


class TestTheDescriptionsLanguage:
    """#215: it is read by a person, in the interface they chose."""

    async def test_the_explicit_describe_writes_in_the_requested_language(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prompts: list[str] = []

        async def fake_generate_text(*args: object, prompt: str = "", **kwargs: object) -> str:
            prompts.append(prompt)
            return "Фінансові показники Apple."

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
        monkeypatch.setattr("contextvault.services.report_llm.generate_text", fake_generate_text)

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_lang_1")
        token = await _token(client, "admin_lang_1")
        connection_id = await _connect_with_schema(client, repo.id, token)

        resp = await client.post(
            f"/repositories/{repo.id}/databases/{connection_id}/describe?language=uk",
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert "Ukrainian" in prompts[-1]
        assert resp.json()["description"] == "Фінансові показники Apple."

    async def test_the_queued_description_carries_the_saves_language(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A background task has no reader to ask, so the save has to tell it."""
        queued: list[tuple[object, ...]] = []

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)
        monkeypatch.setattr(
            "contextvault.api.database.run_connection_description",
            lambda *args: queued.append(args),
        )

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_lang_2")
        token = await _token(client, "admin_lang_2")
        created = await client.post(
            f"/repositories/{repo.id}/databases",
            json=_payload(name="Reporting"),
            headers=_auth(token),
        )
        connection_id = str(created.json()["id"])
        await client.patch(
            f"/repositories/{repo.id}/databases/{connection_id}/schema",
            json={
                "exposed_schema": [
                    {"table": "orders", "description": "Customer orders", "columns": []}
                ],
                "language": "uk",
            },
            headers=_auth(token),
        )

        assert len(queued) == 1
        assert queued[0][1] == "uk"

    async def test_a_save_without_a_language_still_succeeds(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The field is optional: an older client, or none at all, must not turn
        an allow-list save into a 422 over a locale string."""

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_lang_3")
        token = await _token(client, "admin_lang_3")
        connection_id = await _connect_with_schema(client, repo.id, token)
        assert connection_id

    async def test_a_longer_correction_is_accepted(
        self, db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#215 raised the generated cap; a hand-written correction must not be
        rejected for being the same length as what it replaces."""

        async def fake_test_connection(**kwargs: object) -> None:
            return None

        monkeypatch.setattr("contextvault.api.database.test_connection", fake_test_connection)

        repo = await _repo_with_llm(db_session)
        await _user(db_session, Role.ADMIN, "admin_lang_4")
        token = await _token(client, "admin_lang_4")
        connection_id = await _connect_with_schema(client, repo.id, token)

        long_text = "Ф" * 400
        resp = await client.patch(
            f"/repositories/{repo.id}/databases/{connection_id}/description",
            json={"description": long_text},
            headers=_auth(token),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == long_text
