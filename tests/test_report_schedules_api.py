"""Report-schedules API: freeze a DONE report into a
nightly schedule, list, toggle, delete.

Mirrors ``test_reports_api``'s fixture pattern. A schedule is created by *freezing*
an existing ``GeneratedReport`` row — created directly in the DB here, with
``generated_sql``/``chart_spec`` set, standing in for a report that finished
generation via the reports API (Task 10/11). No background task is involved, so
no monkeypatching is needed.
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import (
    DatabaseConnection,
    DatabaseType,
    GeneratedReport,
    Grant,
    ReportSchedule,
    ReportStatus,
    Repository,
    Role,
    User,
)
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


async def _repo(db_session: AsyncSession) -> Repository:
    repo = Repository(name="Vault", llm_provider="gemini", llm_model="gem-1")
    db_session.add(repo)
    await db_session.flush()
    return repo


async def _connection(db_session: AsyncSession, repository_id: uuid.UUID) -> DatabaseConnection:
    conn = DatabaseConnection(
        repository_id=repository_id,
        name="Reporting",
        db_type=DatabaseType.POSTGRES,
        host="localhost",
        port=5432,
        database="reporting",
        username="ro_user",
        password_encrypted=encrypt("secret"),
        exposed_schema=[],
    )
    db_session.add(conn)
    await db_session.flush()
    return conn


async def _grant(db_session: AsyncSession, user_id: uuid.UUID, repo_id: uuid.UUID) -> None:
    db_session.add(Grant(user_id=user_id, repository_id=repo_id))
    await db_session.flush()


_UNSET = object()


async def _report(
    db_session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    requested_by: uuid.UUID,
    status: ReportStatus = ReportStatus.DONE,
    generated_sql: str | None = "SELECT count(*) FROM users",
    chart_spec: dict[str, str] | None | object = _UNSET,
    prompt: str = "How many users signed up last week?",
) -> GeneratedReport:
    if chart_spec is _UNSET:
        chart_spec = (
            {"type": "bar", "x": "day", "y": "count"}
            if status == ReportStatus.DONE and generated_sql is not None
            else None
        )
    report = GeneratedReport(
        repository_id=repository_id,
        connection_id=connection_id,
        requested_by=requested_by,
        prompt=prompt,
        status=status,
        generated_sql=generated_sql,
        chart_spec=chart_spec,
    )
    db_session.add(report)
    await db_session.flush()
    return report


async def _schedule(
    db_session: AsyncSession,
    *,
    repository_id: uuid.UUID,
    connection_id: uuid.UUID,
    owner_id: uuid.UUID | None,
    run_at_time: str = "01:00",
    enabled: bool = True,
    prompt: str = "frozen report",
) -> ReportSchedule:
    import datetime as dt

    schedule = ReportSchedule(
        repository_id=repository_id,
        connection_id=connection_id,
        owner_id=owner_id,
        prompt=prompt,
        frozen_sql="SELECT 1",
        frozen_chart_spec={"type": "bar"},
        run_at_time=dt.time.fromisoformat(run_at_time),
        enabled=enabled,
    )
    db_session.add(schedule)
    await db_session.flush()
    return schedule


# --------------------------------------------------------------------------- #
# Creation (freeze)
# --------------------------------------------------------------------------- #


async def test_create_schedule_201_freezes_from_done_report(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    report = await _report(
        db_session, repository_id=repo.id, connection_id=conn.id, requested_by=alice.id
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["repository_id"] == str(repo.id)
    assert body["prompt"] == report.prompt
    assert body["run_at_time"] == "01:00:00"
    assert body["enabled"] is True
    assert body["last_run_at"] is None
    assert body["last_error"] is None

    schedule = await db_session.get(ReportSchedule, uuid.UUID(body["id"]))
    assert schedule is not None
    assert schedule.frozen_sql == report.generated_sql
    assert schedule.frozen_chart_spec == report.chart_spec
    assert schedule.connection_id == conn.id
    assert schedule.owner_id == alice.id
    assert schedule.repository_id == repo.id


async def test_create_schedule_admin_can_freeze_others_report(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    admin = await _user(db_session, Role.ADMIN, "root")
    report = await _report(
        db_session, repository_id=repo.id, connection_id=conn.id, requested_by=alice.id
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "02:30"},
        headers=_auth(await _token(client, "root")),
    )
    assert resp.status_code == 201
    schedule = await db_session.get(ReportSchedule, uuid.UUID(resp.json()["id"]))
    assert schedule is not None
    assert schedule.owner_id == admin.id


@pytest.mark.parametrize("bad_status", [ReportStatus.PENDING, ReportStatus.FAILED])
async def test_create_schedule_400_report_not_done(
    db_session: AsyncSession, client: AsyncClient, bad_status: ReportStatus
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    report = await _report(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=alice.id,
        status=bad_status,
        generated_sql=None,
        chart_spec=None,
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_schedule_400_missing_generated_sql(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    report = await _report(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=alice.id,
        generated_sql=None,
        chart_spec={"type": "bar"},
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_schedule_400_missing_chart_spec(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    report = await _report(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=alice.id,
        generated_sql="SELECT 1",
        chart_spec=None,
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_schedule_400_report_belongs_to_other_user(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)
    report = await _report(
        db_session, repository_id=repo.id, connection_id=conn.id, requested_by=bob.id
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_schedule_400_report_in_different_repo(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    other_repo = await _repo(db_session)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, alice.id, other_repo.id)
    report = await _report(
        db_session, repository_id=other_repo.id, connection_id=conn.id, requested_by=alice.id
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_schedule_400_unknown_report_id(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(uuid.uuid4()), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_schedule_404_unknown_repo(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.USER, "alice")
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/report-schedules",
        json={"report_id": str(uuid.uuid4()), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


async def test_create_schedule_403_ungranted_user(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")  # no grant
    report = await _report(
        db_session, repository_id=repo.id, connection_id=conn.id, requested_by=alice.id
    )

    resp = await client.post(
        f"/repositories/{repo.id}/report-schedules",
        json={"report_id": str(report.id), "run_at_time": "01:00"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Listing: own-only vs admin ?all=true
# --------------------------------------------------------------------------- #


async def test_list_schedules_is_own_only_newest_first(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)

    first = await _schedule(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        owner_id=alice.id,
        prompt="alice schedule one",
    )
    second = await _schedule(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        owner_id=alice.id,
        prompt="alice schedule two",
    )
    await _schedule(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        owner_id=bob.id,
        prompt="bob schedule",
    )

    base = datetime.now(UTC)
    await db_session.execute(
        sa.update(ReportSchedule).where(ReportSchedule.id == first.id).values(created_at=base)
    )
    await db_session.execute(
        sa.update(ReportSchedule)
        .where(ReportSchedule.id == second.id)
        .values(created_at=base + timedelta(seconds=1))
    )
    await db_session.commit()

    resp = await client.get(
        f"/repositories/{repo.id}/report-schedules", headers=_auth(await _token(client, "alice"))
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [s["prompt"] for s in body] == ["alice schedule two", "alice schedule one"]


async def test_list_schedules_all_true_admin_only(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    await _user(db_session, Role.ADMIN, "root")
    await _schedule(db_session, repository_id=repo.id, connection_id=conn.id, owner_id=alice.id)

    forbidden = await client.get(
        f"/repositories/{repo.id}/report-schedules?all=true",
        headers=_auth(await _token(client, "alice")),
    )
    assert forbidden.status_code == 403

    listing = await client.get(
        f"/repositories/{repo.id}/report-schedules?all=true",
        headers=_auth(await _token(client, "root")),
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1


async def test_list_schedules_403_ungranted_user(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    await _user(db_session, Role.USER, "alice")  # no grant
    resp = await client.get(
        f"/repositories/{repo.id}/report-schedules", headers=_auth(await _token(client, "alice"))
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# PATCH: toggle + reschedule
# --------------------------------------------------------------------------- #


async def test_patch_schedule_toggles_enabled_and_run_at_time(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    schedule = await _schedule(
        db_session, repository_id=repo.id, connection_id=conn.id, owner_id=alice.id
    )

    resp = await client.patch(
        f"/report-schedules/{schedule.id}",
        json={"enabled": False, "run_at_time": "05:15"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["run_at_time"] == "05:15:00"


async def test_patch_schedule_applies_only_provided_fields(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    schedule = await _schedule(
        db_session,
        repository_id=repo.id,
        connection_id=conn.id,
        owner_id=alice.id,
        run_at_time="01:00",
        enabled=True,
    )

    resp = await client.patch(
        f"/report-schedules/{schedule.id}",
        json={"enabled": False},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["run_at_time"] == "01:00:00"


async def test_patch_other_users_schedule_is_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)
    schedule = await _schedule(
        db_session, repository_id=repo.id, connection_id=conn.id, owner_id=bob.id
    )

    resp = await client.patch(
        f"/report-schedules/{schedule.id}",
        json={"enabled": False},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


async def test_admin_can_patch_any_users_schedule(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    await _user(db_session, Role.ADMIN, "root")
    schedule = await _schedule(
        db_session, repository_id=repo.id, connection_id=conn.id, owner_id=alice.id
    )

    resp = await client.patch(
        f"/report-schedules/{schedule.id}",
        json={"enabled": False},
        headers=_auth(await _token(client, "root")),
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


async def test_patch_unknown_schedule_404(db_session: AsyncSession, client: AsyncClient) -> None:
    await _user(db_session, Role.USER, "alice")
    resp = await client.patch(
        f"/report-schedules/{uuid.uuid4()}",
        json={"enabled": False},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


async def test_delete_own_schedule_204(db_session: AsyncSession, client: AsyncClient) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    schedule = await _schedule(
        db_session, repository_id=repo.id, connection_id=conn.id, owner_id=alice.id
    )

    resp = await client.delete(
        f"/report-schedules/{schedule.id}", headers=_auth(await _token(client, "alice"))
    )
    assert resp.status_code == 204

    remaining = await db_session.get(ReportSchedule, schedule.id)
    assert remaining is None


async def test_delete_other_users_schedule_404(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)
    schedule = await _schedule(
        db_session, repository_id=repo.id, connection_id=conn.id, owner_id=bob.id
    )

    resp = await client.delete(
        f"/report-schedules/{schedule.id}", headers=_auth(await _token(client, "alice"))
    )
    assert resp.status_code == 404


async def test_admin_can_delete_any_users_schedule(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    await _user(db_session, Role.ADMIN, "root")
    schedule = await _schedule(
        db_session, repository_id=repo.id, connection_id=conn.id, owner_id=alice.id
    )

    resp = await client.delete(
        f"/report-schedules/{schedule.id}", headers=_auth(await _token(client, "root"))
    )
    assert resp.status_code == 204
