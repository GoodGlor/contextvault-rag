"""Reports API: request, poll, per-user history, PDF.

Also covers the plan review loop (Task 6): ``POST .../reports`` now stops at
``PLANNING`` and dispatches ``run_report_planning``; ``/replan`` and ``/approve``
drive the loop from there.

Mirrors ``test_admin_notes_api``'s fixture pattern. The BackgroundTasks entrypoint
imported into ``contextvault.api.reports`` — ``run_report_planning`` for creation,
``run_report_replanning``/``run_report_generation`` for the two follow-on endpoints
— is monkeypatched per test: a fake that opens the shared ``db_session`` and marks
the report DONE with PDF bytes when a test needs a completed artifact, or a no-op
when a test wants to observe the PLANNING state (e.g. the download-while-pending
404).
"""

import base64
import uuid
import warnings
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

import contextvault.api.reports as reports_api
from contextvault.core.crypto import encrypt
from contextvault.db.session import get_session
from contextvault.main import create_app
from contextvault.models import (
    DatabaseConnection,
    DatabaseType,
    GeneratedReport,
    ReportAsset,
    ReportStatus,
    Repository,
    Role,
    Source,
    SourceKind,
    SourceStatus,
    User,
)
from contextvault.services import users as user_service
from contextvault.services.report_blocks import BLOCKS_VERSION, allowed_chart_types, load_blocks
from contextvault.services.report_execution import QueryResult
from contextvault.services.report_html import build_report_html
from contextvault.services.report_persist import load_sections
from contextvault.services.report_style import TEMPLATES, resolve_style
from contextvault.services.report_types import SectionResult, SectionSourceRef
from contextvault.services.reports import ReportNotRenderable, _store_sections


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


async def _connection(
    db_session: AsyncSession, repository_id: uuid.UUID, *, name: str = "Reporting"
) -> DatabaseConnection:
    conn = DatabaseConnection(
        repository_id=repository_id,
        name=name,
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


async def _grant(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    repo_id: uuid.UUID,
    *,
    expires_at: datetime | None = None,
) -> None:
    from contextvault.models import Grant

    db_session.add(Grant(user_id=user_id, repository_id=repo_id, expires_at=expires_at))
    await db_session.flush()


def _make_completing_fake(db_session: AsyncSession, calls: list[uuid.UUID]):  # type: ignore[no-untyped-def]
    """A fake background entrypoint that marks the report DONE with PDF bytes."""

    async def fake(report_id: uuid.UUID) -> None:
        calls.append(report_id)
        report = await db_session.get(GeneratedReport, report_id)
        assert report is not None
        report.status = ReportStatus.DONE
        report.generated_sql = "SELECT count(*) FROM users"
        report.pdf_data = b"%PDF-fake"
        report.pdf_filename = f"report-{report_id}.pdf"
        await db_session.commit()

    return fake


def _make_noop_fake(calls: list[uuid.UUID]):  # type: ignore[no-untyped-def]
    """A fake background entrypoint that records the call but leaves PLANNING."""

    async def fake(report_id: uuid.UUID) -> None:
        calls.append(report_id)

    return fake


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #


async def test_create_report_201_dispatches_background(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)

    calls: list[uuid.UUID] = []
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake(calls))

    resp = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "How many users signed up last week?"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["repository_id"] == str(repo.id)
    assert body["prompt"] == "How many users signed up last week?"
    assert body["status"] == "planning"
    assert body["has_pdf"] is False
    assert body["schedule_id"] is None
    assert "generated_sql" not in body
    assert calls == [uuid.UUID(body["id"])]


async def test_create_report_201_with_several_connections(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository holding several databases can still request a report.

    #201 made a second connection representable; this endpoint still resolved the
    repository's connection with ``scalar_one_or_none()``, which raises
    ``MultipleResultsFound`` the moment there are two.
    """
    repo = await _repo(db_session)
    await _connection(db_session, repo.id, name="Apple")
    await _connection(db_session, repo.id, name="Microsoft")
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    resp = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "Compare Apple and Microsoft revenue"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 201


async def test_create_report_400_without_connection(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)  # no DatabaseConnection
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    resp = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "anything"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 400


async def test_create_report_404_unknown_repo(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    await _user(db_session, Role.USER, "alice")
    resp = await client.post(
        f"/repositories/{uuid.uuid4()}/reports",
        json={"prompt": "anything"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


async def test_create_report_403_ungranted_user(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    await _user(db_session, Role.USER, "alice")  # no grant
    resp = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "anything"},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 403


async def test_create_report_rejects_empty_prompt(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    resp = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": ""},
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# Listing: own-only vs admin ?all=true
# --------------------------------------------------------------------------- #


async def test_list_reports_is_own_only_newest_first(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    alice_token = await _token(client, "alice")
    bob_token = await _token(client, "bob")

    first = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "alice report one"},
        headers=_auth(alice_token),
    )
    second = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "alice report two"},
        headers=_auth(alice_token),
    )
    await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "bob report"},
        headers=_auth(bob_token),
    )
    assert first.status_code == second.status_code == 201

    # Postgres's `now()` resolves to the transaction's start time, and this whole
    # test runs inside the single shared ``db_session`` transaction — so every row
    # created here gets an identical ``created_at`` from the server default. Force
    # distinct timestamps directly so "newest first" is observable within one test
    # transaction; in production, separate requests run in separate transactions
    # and naturally get distinct real-clock values.
    base = datetime.now(UTC)
    await db_session.execute(
        sa.update(GeneratedReport)
        .where(GeneratedReport.id == uuid.UUID(first.json()["id"]))
        .values(created_at=base)
    )
    await db_session.execute(
        sa.update(GeneratedReport)
        .where(GeneratedReport.id == uuid.UUID(second.json()["id"]))
        .values(created_at=base + timedelta(seconds=1))
    )
    await db_session.commit()

    listing = await client.get(f"/repositories/{repo.id}/reports", headers=_auth(alice_token))
    assert listing.status_code == 200
    body = listing.json()
    assert [r["prompt"] for r in body] == ["alice report two", "alice report one"]
    assert all("generated_sql" not in r for r in body)


async def test_get_other_users_report_is_404_not_403(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    bob_token = await _token(client, "bob")
    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "bob's private report"},
        headers=_auth(bob_token),
    )
    report_id = created.json()["id"]

    resp = await client.get(
        f"/repositories/{repo.id}/reports/{report_id}",
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


async def test_list_all_true_is_admin_only_and_includes_generated_sql(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    await _user(db_session, Role.ADMIN, "root")

    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        reports_api, "run_report_planning", _make_completing_fake(db_session, calls)
    )

    alice_token = await _token(client, "alice")
    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "alice's report"},
        headers=_auth(alice_token),
    )
    assert created.status_code == 201

    # A non-admin passing ?all=true is forbidden, even though it is their own repo access.
    forbidden = await client.get(
        f"/repositories/{repo.id}/reports?all=true", headers=_auth(alice_token)
    )
    assert forbidden.status_code == 403

    admin_token = await _token(client, "root")
    listing = await client.get(
        f"/repositories/{repo.id}/reports?all=true", headers=_auth(admin_token)
    )
    assert listing.status_code == 200
    body = listing.json()
    assert len(body) == 1
    assert body[0]["prompt"] == "alice's report"
    assert body[0]["generated_sql"] == "SELECT count(*) FROM users"
    assert body[0]["status"] == "done"
    assert body[0]["has_pdf"] is True


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


async def test_download_404_while_pending_then_200_once_done(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "alice")

    calls: list[uuid.UUID] = []
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake(calls))
    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "pending report"},
        headers=_auth(token),
    )
    report_id = created.json()["id"]

    pending_download = await client.get(
        f"/repositories/{repo.id}/reports/{report_id}/download", headers=_auth(token)
    )
    assert pending_download.status_code == 404

    # Now mark it DONE with PDF bytes directly (simulating generation finishing).
    report = await db_session.get(GeneratedReport, uuid.UUID(report_id))
    assert report is not None
    report.status = ReportStatus.DONE
    report.pdf_data = b"%PDF-fake-bytes"
    report.pdf_filename = "report-x.pdf"
    await db_session.commit()

    done_download = await client.get(
        f"/repositories/{repo.id}/reports/{report_id}/download", headers=_auth(token)
    )
    assert done_download.status_code == 200
    assert done_download.headers["content-type"] == "application/pdf"
    assert done_download.headers["content-disposition"] == 'attachment; filename="report-x.pdf"'
    assert done_download.content == b"%PDF-fake-bytes"


async def test_download_format_selects_the_artifact(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader picks the format; a report without a PPTX 404s rather than
    handing back a PDF under a .pptx name."""
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "alice")

    calls: list[uuid.UUID] = []
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake(calls))
    created = await client.post(
        f"/repositories/{repo.id}/reports", json={"prompt": "p"}, headers=_auth(token)
    )
    report_id = created.json()["id"]
    report = await db_session.get(GeneratedReport, uuid.UUID(report_id))
    assert report is not None
    report.status = ReportStatus.DONE
    report.pdf_data = b"%PDF-fake"
    report.pdf_filename = "report-x.pdf"
    await db_session.commit()

    base = f"/repositories/{repo.id}/reports/{report_id}"
    # PDF-only report: pptx is genuinely unavailable.
    assert (
        await client.get(f"{base}/download?format=pptx", headers=_auth(token))
    ).status_code == 404
    listing = await client.get(f"/repositories/{repo.id}/reports", headers=_auth(token))
    row = listing.json()[0]
    assert row["has_pdf"] is True and row["has_pptx"] is False

    report.pptx_data = b"PK-fake-deck"
    report.pptx_filename = "report-x.pptx"
    await db_session.commit()

    pptx = await client.get(f"{base}/download?format=pptx", headers=_auth(token))
    assert pptx.status_code == 200
    assert pptx.content == b"PK-fake-deck"
    assert pptx.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert pptx.headers["content-disposition"] == 'attachment; filename="report-x.pptx"'

    # No format parameter still means PDF, so existing links keep working.
    default = await client.get(f"{base}/download", headers=_auth(token))
    assert default.status_code == 200 and default.content == b"%PDF-fake"


async def test_download_pdf_bytes_never_appear_in_json_listing(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "alice")

    calls: list[uuid.UUID] = []
    monkeypatch.setattr(
        reports_api, "run_report_planning", _make_completing_fake(db_session, calls)
    )
    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "report with pdf"},
        headers=_auth(token),
    )
    assert created.status_code == 201

    listing = await client.get(f"/repositories/{repo.id}/reports", headers=_auth(token))
    assert listing.status_code == 200
    raw_text = listing.text
    assert "%PDF" not in raw_text
    body = listing.json()
    assert "pdf_data" not in body[0]
    assert body[0]["has_pdf"] is True


async def test_download_403_for_ungranted_stranger_repo_visible_only_via_grant(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Sanity: repository existence still gates as 404 for an unknown repo id on
    the download route (routed through the same owner/admin lookup)."""
    await _user(db_session, Role.USER, "alice")
    resp = await client.get(
        f"/repositories/{uuid.uuid4()}/reports/{uuid.uuid4()}/download",
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


async def test_delete_own_report_204(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    user = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, user.id, repo.id)
    token = await _token(client, "alice")
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "to delete"},
        headers=_auth(token),
    )
    report_id = created.json()["id"]

    resp = await client.delete(f"/repositories/{repo.id}/reports/{report_id}", headers=_auth(token))
    assert resp.status_code == 204

    follow_up = await client.get(
        f"/repositories/{repo.id}/reports/{report_id}", headers=_auth(token)
    )
    assert follow_up.status_code == 404


async def test_delete_other_users_report_404(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    bob = await _user(db_session, Role.USER, "bob")
    await _grant(db_session, alice.id, repo.id)
    await _grant(db_session, bob.id, repo.id)
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    bob_token = await _token(client, "bob")
    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "bob's report"},
        headers=_auth(bob_token),
    )
    report_id = created.json()["id"]

    resp = await client.delete(
        f"/repositories/{repo.id}/reports/{report_id}",
        headers=_auth(await _token(client, "alice")),
    )
    assert resp.status_code == 404


async def test_admin_can_delete_any_users_report(
    db_session: AsyncSession, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    alice = await _user(db_session, Role.USER, "alice")
    await _grant(db_session, alice.id, repo.id)
    await _user(db_session, Role.ADMIN, "root")
    monkeypatch.setattr(reports_api, "run_report_planning", _make_noop_fake([]))

    alice_token = await _token(client, "alice")
    created = await client.post(
        f"/repositories/{repo.id}/reports",
        json={"prompt": "alice's report"},
        headers=_auth(alice_token),
    )
    report_id = created.json()["id"]

    resp = await client.delete(
        f"/repositories/{repo.id}/reports/{report_id}",
        headers=_auth(await _token(client, "root")),
    )
    assert resp.status_code == 204


# --------------------------------------------------------------------------- #
# Blended reports: create guard (DB or documents), admin sections audit
# --------------------------------------------------------------------------- #


async def _noop_planning(report_id: uuid.UUID) -> None:
    return None


async def test_create_report_on_docs_only_repo_succeeds(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repo with DONE sources but no DB connection can request a report."""
    monkeypatch.setattr(reports_api, "run_report_planning", _noop_planning)
    await _user(db_session, Role.ADMIN, "admin-docs")
    repo = Repository(name="Docs only")
    db_session.add(repo)
    await db_session.flush()
    db_session.add(
        Source(
            repository_id=repo.id,
            title="policy.pdf",
            kind=SourceKind.DOCUMENT,
            status=SourceStatus.DONE,
        )
    )
    await db_session.commit()
    headers = _auth(await _token(client, "admin-docs"))

    resp = await client.post(
        f"/repositories/{repo.id}/reports", json={"prompt": "огляд"}, headers=headers
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "planning"


async def test_create_report_rejected_when_no_db_and_no_done_sources(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed-only sources don't count — nothing is retrievable from them."""
    monkeypatch.setattr(reports_api, "run_report_planning", _noop_planning)
    await _user(db_session, Role.ADMIN, "admin-broken")
    repo = Repository(name="Broken uploads")
    db_session.add(repo)
    await db_session.flush()
    db_session.add(
        Source(
            repository_id=repo.id,
            title="bad.pdf",
            kind=SourceKind.DOCUMENT,
            status=SourceStatus.FAILED,
        )
    )
    await db_session.commit()
    headers = _auth(await _token(client, "admin-broken"))

    resp = await client.post(
        f"/repositories/{repo.id}/reports", json={"prompt": "огляд"}, headers=headers
    )
    assert resp.status_code == 400
    assert "add sources" in resp.json()["detail"]


async def test_admin_all_view_exposes_sections_audit(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _user(db_session, Role.ADMIN, "admin-audit")
    repo = Repository(name="Audit vault")
    db_session.add(repo)
    await db_session.flush()
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=None,
        requested_by=None,
        prompt="огляд",
        status=ReportStatus.DONE,
        sections=[
            {
                "title": "A",
                "source": "documents",
                "sub_question": "q",
                "status": "done",
                "error": None,
                "generated_sql": None,
                "chart_spec": None,
                "cited_source_ids": [],
            }
        ],
    )
    db_session.add(report)
    await db_session.commit()
    headers = _auth(await _token(client, "admin-audit"))

    resp = await client.get(
        f"/repositories/{repo.id}/reports", params={"all": "true"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()[0]["sections"][0]["source"] == "documents"


def _draft(conn: DatabaseConnection) -> dict[str, Any]:
    """A complete plan draft, routed at this repository's database.

    Since #203 a database section names the connection it reads, and the server
    checks that id against the repository — so the draft has to be built per
    test rather than shared as a constant.
    """
    return {
        "title": "Дохід",
        "language": "uk",
        "sections": [
            {
                "title": "Динаміка",
                "source": "database",
                "databases": [str(conn.id)],
                "sub_question": "дохід за FY25?",
            }
        ],
    }


async def test_create_report_stops_at_planning(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requesting a report plans it; it must not start generating."""
    admin = await _user(db_session, Role.ADMIN, "admin1")
    repo = await _repo(db_session)
    await _connection(db_session, repo.id)
    await db_session.commit()

    called: list[str] = []

    async def fake_planning(report_id: uuid.UUID, **kwargs: object) -> None:
        called.append("planning")

    async def fake_generation(report_id: uuid.UUID, **kwargs: object) -> None:
        called.append("generation")

    monkeypatch.setattr(reports_api, "run_report_planning", fake_planning)
    monkeypatch.setattr(reports_api, "run_report_generation", fake_generation)

    token = await _token(client, admin.username)
    resp = await client.post(
        f"/repositories/{repo.id}/reports", json={"prompt": "дохід"}, headers=_auth(token)
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "planning"
    assert resp.json()["plan"] is None
    assert resp.json()["plan_rounds"] == 0
    assert called == ["planning"]


async def test_replan_and_approve_require_awaiting_approval(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Neither endpoint can act on a report that isn't waiting on its owner — in
    particular, one already PROCESSING cannot be re-approved into a second
    generation run."""
    admin = await _user(db_session, Role.ADMIN, "admin2")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id, requested_by=admin.id, prompt="p", status=ReportStatus.PROCESSING
    )
    db_session.add(report)
    await db_session.commit()

    token = await _token(client, admin.username)
    for action in ("replan", "approve"):
        resp = await client.post(
            f"/repositories/{repo.id}/reports/{report.id}/{action}",
            json=_draft(conn),
            headers=_auth(token),
        )
        assert resp.status_code == 409, action


async def test_replan_dispatches_and_returns_to_planning(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await _user(db_session, Role.ADMIN, "admin3")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
        plan_rounds=1,
    )
    db_session.add(report)
    await db_session.commit()

    seen: list[dict[str, Any]] = []

    async def fake_replanning(
        report_id: uuid.UUID, draft_data: dict[str, Any], **kwargs: object
    ) -> None:
        seen.append(draft_data)

    monkeypatch.setattr(reports_api, "run_report_replanning", fake_replanning)

    token = await _token(client, admin.username)
    edited = {
        "title": "Дохід",
        "language": "uk",
        "sections": [
            {
                "title": "Динаміка",
                "source": "database",
                "databases": [str(conn.id)],
                "sub_question": "дохід за FY25?",
            },
            {"title": "Маржа", "source": "database", "sub_question": ""},
        ],
    }
    resp = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/replan", json=edited, headers=_auth(token)
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "planning"
    assert len(seen) == 1
    assert seen[0]["sections"][1]["sub_question"] == ""


async def test_approve_starts_generation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await _user(db_session, Role.ADMIN, "admin4")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
        plan_rounds=1,
    )
    db_session.add(report)
    await db_session.commit()

    called: list[str] = []

    async def fake_generation(report_id: uuid.UUID, **kwargs: object) -> None:
        called.append("generation")

    monkeypatch.setattr(reports_api, "run_report_generation", fake_generation)

    token = await _token(client, admin.username)
    resp = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/approve",
        json=_draft(conn),
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    assert resp.json()["plan"]["sections"][0]["source"] == "database"
    assert called == ["generation"]

    await db_session.refresh(report)
    assert report.plan is not None
    assert report.plan["sections"][0]["sub_question"] == "дохід за FY25?"


async def test_approve_rejects_a_blank_field_naming_the_section(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin = await _user(db_session, Role.ADMIN, "admin5")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    token = await _token(client, admin.username)
    blank = {
        "title": "Дохід",
        "language": "uk",
        "sections": [{"title": "Маржа", "source": "database", "sub_question": ""}],
    }
    resp = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/approve", json=blank, headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "Маржа" in resp.json()["detail"]


async def test_approve_rejects_a_source_the_repository_lacks(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A hand-crafted plan is untrusted input, not LLM output that was already checked."""
    admin = await _user(db_session, Role.ADMIN, "admin6")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    token = await _token(client, admin.username)
    docs = {
        "title": "Дохід",
        "language": "uk",
        "sections": [{"title": "Політика", "source": "documents", "sub_question": "правила?"}],
    }
    resp = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/approve", json=docs, headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "no documents" in resp.json()["detail"]


async def test_replan_rejects_a_source_the_repository_lacks(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same untrusted-input guard as ``/approve``: a hand-crafted section naming
    a source this repository lacks is rejected before anything is scheduled, rather
    than burning the whole repair loop on something the model cannot fix."""
    admin = await _user(db_session, Role.ADMIN, "admin9")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    called: list[str] = []

    async def fake_replanning(
        report_id: uuid.UUID, draft_data: dict[str, Any], **kwargs: object
    ) -> None:
        called.append("replanning")

    monkeypatch.setattr(reports_api, "run_report_replanning", fake_replanning)

    token = await _token(client, admin.username)
    docs = {
        "title": "Дохід",
        "language": "uk",
        "sections": [{"title": "Політика", "source": "documents", "sub_question": "правила?"}],
    }
    resp = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/replan", json=docs, headers=_auth(token)
    )
    assert resp.status_code == 400
    assert "no documents" in resp.json()["detail"]
    assert called == []

    await db_session.refresh(report)
    assert report.status == ReportStatus.AWAITING_APPROVAL


async def test_second_approve_gets_409_and_generation_dispatched_once(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two ``/approve`` posts on the same row — a UI double-click is enough — must
    not both reach generation. The transition to ``PENDING`` is a conditional
    write, so the loser 409s instead of dispatching a second pipeline.

    The ``client``/``db_session`` fixtures share one ``AsyncSession`` across every
    request in a test, so the second request's own pre-check
    (``_awaiting_or_409``) would already 409 it against the identity-mapped
    object the first request just refreshed to ``PENDING`` — that would make this
    test pass even with a naive read-then-write and prove nothing about the fix.
    ``set_committed_value`` forces the in-memory object back to
    ``AWAITING_APPROVAL`` *without* marking it dirty (so nothing gets flushed,
    unlike a plain attribute assignment), simulating what a genuinely concurrent
    session would have read before the first request committed. That makes the
    second request's own pre-check pass, so its conditional ``UPDATE`` has to
    meet the real, already-``PENDING`` row in the database — exercising the
    rowcount-0 / 409 branch this test exists to cover.
    """
    admin = await _user(db_session, Role.ADMIN, "admin10")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    called: list[str] = []

    async def fake_generation(report_id: uuid.UUID, **kwargs: object) -> None:
        called.append("generation")

    monkeypatch.setattr(reports_api, "run_report_generation", fake_generation)

    token = await _token(client, admin.username)
    first = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/approve",
        json=_draft(conn),
        headers=_auth(token),
    )
    assert first.status_code == 200

    set_committed_value(report, "status", ReportStatus.AWAITING_APPROVAL)

    second = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/approve",
        json=_draft(conn),
        headers=_auth(token),
    )
    assert second.status_code == 409
    assert called == ["generation"]


async def test_replan_loses_to_a_concurrent_approve(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``/replan`` racing a winning ``/approve`` must not flush ``PLANNING`` back
    over the ``PENDING`` the approve just committed — that would let generation and
    re-planning both run against the same row, since ``generate_report`` never
    checks that the plan it executes was actually approved. Same
    ``set_committed_value`` technique as the double-approve test: force the
    in-memory row back to ``AWAITING_APPROVAL`` after ``/approve`` commits so
    ``/replan``'s own pre-check passes and its conditional ``UPDATE`` meets the
    real ``PENDING`` row.
    """
    admin = await _user(db_session, Role.ADMIN, "admin11")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=admin.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    generation_calls: list[str] = []
    replanning_calls: list[str] = []

    async def fake_generation(report_id: uuid.UUID, **kwargs: object) -> None:
        generation_calls.append("generation")

    async def fake_replanning(
        report_id: uuid.UUID, draft_data: dict[str, Any], **kwargs: object
    ) -> None:
        replanning_calls.append("replanning")

    monkeypatch.setattr(reports_api, "run_report_generation", fake_generation)
    monkeypatch.setattr(reports_api, "run_report_replanning", fake_replanning)

    token = await _token(client, admin.username)
    approved = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/approve",
        json=_draft(conn),
        headers=_auth(token),
    )
    assert approved.status_code == 200

    set_committed_value(report, "status", ReportStatus.AWAITING_APPROVAL)

    replanned = await client.post(
        f"/repositories/{repo.id}/reports/{report.id}/replan",
        json=_draft(conn),
        headers=_auth(token),
    )
    assert replanned.status_code == 409
    assert generation_calls == ["generation"]
    assert replanning_calls == []

    await db_session.refresh(report)
    assert report.status == ReportStatus.PENDING


async def test_replan_and_approve_hide_another_users_report(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner = await _user(db_session, Role.USER, "owner1")
    stranger = await _user(db_session, Role.USER, "stranger1")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=owner.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    token = await _token(client, stranger.username)
    for action in ("replan", "approve"):
        resp = await client.post(
            f"/repositories/{repo.id}/reports/{report.id}/{action}",
            json=_draft(conn),
            headers=_auth(token),
        )
        assert resp.status_code == 404, action


async def test_replan_and_approve_403_when_grant_expired(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A non-admin whose grant expired while their report sat at AWAITING_APPROVAL
    cannot use either endpoint to run fresh work against the repository — both
    endpoints can dispatch a background task against the reporting database, so
    they re-check the grant, not just ownership."""
    owner = await _user(db_session, Role.USER, "owner2")
    repo = await _repo(db_session)
    conn = await _connection(db_session, repo.id)
    await _grant(db_session, owner.id, repo.id, expires_at=datetime.now(UTC) - timedelta(hours=1))
    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=owner.id,
        prompt="p",
        status=ReportStatus.AWAITING_APPROVAL,
        plan=_draft(conn),
    )
    db_session.add(report)
    await db_session.commit()

    token = await _token(client, owner.username)
    for action in ("replan", "approve"):
        resp = await client.post(
            f"/repositories/{repo.id}/reports/{report.id}/{action}",
            json=_draft(conn),
            headers=_auth(token),
        )
        assert resp.status_code == 403, action


# --------------------------------------------------------------------------- #
# Rerender: rebuild both artifacts from stored section data (Task 6)
# --------------------------------------------------------------------------- #


def _one_data_section() -> SectionResult:
    return SectionResult(
        title="Виторг",
        source="database",
        sub_question="Скільки?",
        kind="data",
        result=QueryResult(columns=["fy", "revenue"], rows=[(2024, Decimal("391.0"))]),
        chart_spec={"chart_type": "bar", "x_column": "fy", "y_column": "revenue"},
    )


@pytest.fixture
def rerender_calls() -> list[uuid.UUID]:
    """Report ids ``fake_rerender`` (below) was actually invoked with.

    Exists so a test can assert the route's central contract — that hitting it
    calls the rebuild — rather than only checking state that was already true
    before the request. A bare ``assert`` inside the fake proves nothing if the
    fake is never called at all; recording the call and asserting on the list
    from the test body is what actually fails if the route stops calling
    ``rerender_report``.
    """
    return []


@pytest.fixture
async def stored_report(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    rerender_calls: list[uuid.UUID],
) -> GeneratedReport:
    """A DONE report whose stored ``sections_data`` alone can rebuild both artifacts.

    Deliberately created with no ``pdf_data``/``pptx_data`` (unlike a real
    ``rerender_report`` payload, which starts from an already-rendered report):
    that makes ``has_pdf`` false beforehand, so a test asserting it turned true
    is a genuine signal that the rebuild ran, not another condition already
    satisfied by the fixture.

    ``rerender_report`` is imported into ``reports_api`` the same way the
    background-task entrypoints are (see this module's docstring), and is
    monkeypatched the same way here: a fake that acts on the shared
    ``db_session`` rather than opening its own. The real function's default
    ``session_factory=SessionLocal`` opens a brand-new connection, which would
    not see this row — ``db_session`` never actually commits to Postgres
    visibly to a second connection until this test's outer transaction ends
    (see ``conftest.db_session``); the real rebuild logic (PDF/PPTX bytes,
    ``ReportNotRenderable``) is already covered end-to-end against a real
    session in ``test_reports_service.py``. Also authenticates ``client`` as
    this report's owner, setting a default ``Authorization`` header on it — the
    rerender tests reuse ``client`` across calls without passing a token each
    time.
    """
    user = await _user(db_session, Role.USER, "rerender-owner")
    token = await _token(client, "rerender-owner")
    client.headers.update(_auth(token))
    repo = await _repo(db_session)
    await _grant(db_session, user.id, repo.id)
    report = GeneratedReport(
        repository_id=repo.id,
        requested_by=user.id,
        prompt="Виторг за 2024",
        status=ReportStatus.DONE,
    )
    _store_sections(report, title="Звіт", language="uk", sections=[_one_data_section()])
    db_session.add(report)
    await db_session.commit()

    async def fake_rerender(report_id: uuid.UUID) -> None:
        rerender_calls.append(report_id)
        target = await db_session.get(GeneratedReport, report_id)
        assert target is not None
        if target.sections_data is None:
            raise ReportNotRenderable(str(report_id))
        target.pdf_data = b"%PDF-rerendered"
        target.pdf_filename = f"report-{report_id}.pdf"
        target.pptx_data = b"PK-rerendered"
        target.pptx_filename = f"report-{report_id}.pptx"
        await db_session.commit()

    monkeypatch.setattr(reports_api, "rerender_report", fake_rerender)
    return report


@pytest.fixture
async def legacy_report(
    db_session: AsyncSession, stored_report: GeneratedReport
) -> GeneratedReport:
    """Same owner and repository as ``stored_report``, but with ``sections_data``
    left unset — the state of every report generated before this change."""
    report = GeneratedReport(
        repository_id=stored_report.repository_id,
        requested_by=stored_report.requested_by,
        prompt="Old report",
        status=ReportStatus.DONE,
        pdf_data=b"%PDF-fake",
        pdf_filename="report-legacy.pdf",
    )
    db_session.add(report)
    await db_session.commit()
    return report


@pytest.fixture
async def other_user_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """A second authenticated client, standing in for a stranger to ``stored_report``."""
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await _user(db_session, Role.USER, "rerender-stranger")
        token = await _token(c, "rerender-stranger")
        c.headers.update(_auth(token))
        yield c


async def test_rerender_returns_the_report_and_keeps_it_done(
    client: AsyncClient, stored_report: GeneratedReport, rerender_calls: list[uuid.UUID]
) -> None:
    response = await client.post(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/rerender"
    )
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["can_rerender"] is True
    # The route's central contract: hitting it actually rebuilds. ``has_pdf`` was
    # false going in (the fixture stores no ``pdf_data``), so it turning true is
    # a state transition the route caused, not a condition already satisfied
    # before the request. The call list is the other half: it fails if the route
    # stops calling ``rerender_report`` at all, which the response alone would not
    # catch (deleting the call but still returning 200/done/can_rerender=True is
    # exactly the gap this pair of assertions closes).
    assert response.json()["has_pdf"] is True
    assert rerender_calls == [stored_report.id]


async def test_rerender_of_a_report_without_stored_data_is_409(
    client: AsyncClient, legacy_report: GeneratedReport
) -> None:
    # legacy_report has sections_data = None, as every report generated before this change does
    response = await client.post(
        f"/repositories/{legacy_report.repository_id}/reports/{legacy_report.id}/rerender"
    )
    assert response.status_code == 409
    assert "cannot be rebuilt" in response.json()["detail"].lower()


async def test_the_list_marks_which_reports_can_be_rebuilt(
    client: AsyncClient, stored_report: GeneratedReport, legacy_report: GeneratedReport
) -> None:
    body = (await client.get(f"/repositories/{stored_report.repository_id}/reports")).json()
    flags = {item["id"]: item["can_rerender"] for item in body}
    assert flags[str(stored_report.id)] is True
    assert flags[str(legacy_report.id)] is False


@pytest.fixture
async def failed_report_with_data(
    db_session: AsyncSession, stored_report: GeneratedReport
) -> GeneratedReport:
    """FAILED, yet carrying section data — the state a renderer crash produces.

    ``_store_sections`` runs before the renderers, so a report that dies while
    rendering has a payload and no artifacts. Offering that report a rebuild
    would hand its owner a PDF for a report they were told had failed.
    """
    report = GeneratedReport(
        repository_id=stored_report.repository_id,
        requested_by=stored_report.requested_by,
        prompt="Впав під час рендера",
        status=ReportStatus.FAILED,
        error="RuntimeError: boom",
    )
    _store_sections(report, title="Звіт", language="uk", sections=[_one_data_section()])
    db_session.add(report)
    await db_session.commit()
    # An INSERT leaves the columns this fixture never set unloaded, so reading one
    # later is a lazy load — blocking I/O under asyncio. A real request gets a
    # fresh session and a full SELECT; this makes the shared test session match.
    await db_session.refresh(report)
    return report


def _selected(statement: Any) -> list[str]:
    """The statement's select list, one expression per item."""
    select_list = str(statement).split("\nFROM")[0].removeprefix("SELECT ")
    return [item.strip() for item in select_list.split(",")]


def test_the_list_query_never_fetches_the_bulk_columns() -> None:
    """The most-polled endpoint in the app must not haul the payloads over.

    ``can_rerender``, ``has_pdf`` and ``has_pptx`` each need one bit, while the
    columns behind them hold a PDF, a PPTX and up to 4 MiB of stored result rows
    per report. The bits are computed in SQL; the columns themselves are never in
    the select list. Asserted on the statement, because a response cannot show
    which columns were transferred to produce it.
    """
    columns = _selected(reports_api._list_statement(GeneratedReport.repository_id == uuid.uuid4()))
    assert "generated_reports.sections_data IS NOT NULL AS has_sections_data" in columns
    assert "generated_reports.pdf_data IS NOT NULL AS has_pdf" in columns
    assert "generated_reports.sections_data" not in columns
    assert "generated_reports.pdf_data" not in columns
    assert "generated_reports.pptx_data" not in columns
    # The admin view adds the audit trail, and still leaves the bulk columns out.
    admin = _selected(
        reports_api._list_statement(GeneratedReport.repository_id == uuid.uuid4(), audit=True)
    )
    assert "generated_reports.sections" in admin
    assert "generated_reports.generated_sql" in admin
    assert "generated_reports.sections_data" not in admin


async def test_a_failed_report_is_never_offered_a_rebuild(
    client: AsyncClient, failed_report_with_data: GeneratedReport
) -> None:
    repository_id = failed_report_with_data.repository_id
    listed = (await client.get(f"/repositories/{repository_id}/reports")).json()
    flags = {item["id"]: item["can_rerender"] for item in listed}
    assert flags[str(failed_report_with_data.id)] is False

    single = (
        await client.get(f"/repositories/{repository_id}/reports/{failed_report_with_data.id}")
    ).json()
    assert single["status"] == "failed"
    assert single["can_rerender"] is False


async def test_another_users_report_is_404_not_403(
    other_user_client: AsyncClient, stored_report: GeneratedReport
) -> None:
    # Deliberate, and established by _get_own_report_or_404: a stranger gets the
    # same answer as for an unknown id, so existence is never revealed. Asserting
    # 403 here would be asserting a leak.
    response = await other_user_client.post(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/rerender"
    )
    assert response.status_code == 404


async def test_a_reader_restyles_their_own_report(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"template": "chasopys"},
    )

    assert response.status_code == 200
    assert response.json()["template"] == "chasopys"


async def test_the_stored_template_appears_in_the_report_list(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    """The list endpoint restricts its columns with ``load_only``.

    A response field whose column is missing from ``_LIST_COLUMNS`` is deferred,
    and reading a deferred attribute on an async session raises MissingGreenlet
    — here and nowhere else, because the single-report route loads the whole row.

    ``expire_all`` between the two calls matters: this suite shares one
    ``db_session`` across "requests" (``expire_on_commit=False``, see
    ``conftest.db_session``), so without it the PUT's own commit would leave
    ``design`` sitting populated in the identity map, and the list query below
    would never need to touch the deferred column at all — the very gap this
    test exists to close would go untested by it. A real request gets a fresh
    session with no such residue; this line makes the shared test session match.

    The URL is built from ids captured *before* ``expire_all``: reading
    ``stored_report.repository_id`` afterwards would itself be an attribute
    access outside any greenlet context and raise the same
    ``MissingGreenlet`` this test means to provoke inside the app instead.
    """
    repository_id, report_id = stored_report.repository_id, stored_report.id
    await client.put(
        f"/repositories/{repository_id}/reports/{report_id}/design",
        json={"template": "mahistral"},
    )
    db_session.expire_all()

    response = await client.get(f"/repositories/{repository_id}/reports")

    assert response.status_code == 200
    assert response.json()[0]["template"] == "mahistral"


async def test_a_report_with_no_stored_design_reports_no_template(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}"
    )

    assert response.status_code == 200
    assert response.json()["template"] is None


async def test_an_unknown_template_is_rejected_on_a_report_too(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"template": "no-such-template"},
    )

    assert response.status_code == 400


async def test_a_stranger_cannot_restyle_someone_elses_report(
    other_user_client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """404, not 403 — the same answer an unknown id gets, so existence leaks nowhere."""
    response = await other_user_client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"template": "minimum"},
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Storing a block list through PUT .../design (Task 9)
# --------------------------------------------------------------------------- #


async def test_a_valid_block_list_is_stored(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "blocks": [{"kind": "cover"}, {"kind": "heading", "section": 0}],
            "blocks_version": BLOCKS_VERSION,
        },
    )

    assert response.status_code == 200
    await db_session.refresh(stored_report)
    assert stored_report.design is not None
    assert stored_report.design["blocks_version"] == BLOCKS_VERSION
    assert [block["kind"] for block in stored_report.design["blocks"]] == ["cover", "heading"]


async def test_blocks_without_a_version_is_rejected(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """``blocks`` and ``blocks_version`` travel together — never an assumed default."""
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": [{"kind": "cover"}]},
    )

    assert response.status_code == 422


async def test_a_422_rejection_raises_no_deprecation_warning(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """``status.HTTP_422_UNPROCESSABLE_ENTITY`` is deprecated in the pinned
    Starlette and warns on every rejected request; the 3 raise sites in
    ``api/reports.py`` must use ``HTTP_422_UNPROCESSABLE_CONTENT`` instead —
    same 422, no warning."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        response = await client.put(
            f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
            json={"blocks": [{"kind": "cover"}]},
        )

    assert response.status_code == 422
    assert not any("UNPROCESSABLE_ENTITY" in str(item.message) for item in caught)


async def test_blocks_with_the_wrong_version_is_rejected(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": [{"kind": "cover"}], "blocks_version": BLOCKS_VERSION + 1},
    )

    assert response.status_code == 422


async def test_blocks_on_a_report_with_no_section_data_is_rejected(
    client: AsyncClient, legacy_report: GeneratedReport
) -> None:
    """A list referencing sections that do not exist would be storing a lie."""
    response = await client.put(
        f"/repositories/{legacy_report.repository_id}/reports/{legacy_report.id}/design",
        json={"blocks": [{"kind": "cover"}], "blocks_version": BLOCKS_VERSION},
    )

    assert response.status_code == 422


async def test_a_misspelled_field_on_a_block_is_rejected_rather_than_silently_dropped(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """``chartType`` instead of ``chart_type`` used to pass validation (``Block``
    carries ``extra="ignore"`` for the read path) and store a block with no
    override — a 200 that silently did nothing. The request schema is now
    ``BlockIn``, which forbids extra keys."""
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "blocks": [{"kind": "viz", "section": 0, "variant": "chart", "chartType": "pie"}],
            "blocks_version": BLOCKS_VERSION,
        },
    )

    assert response.status_code == 422


async def test_an_unknown_kind_is_rejected_and_named(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": [{"kind": "hologram"}], "blocks_version": BLOCKS_VERSION},
    )

    assert response.status_code == 422
    assert "hologram" in response.json()["detail"]


async def test_blocks_null_clears_the_list_and_leaves_the_template(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "template": "chasopys",
            "blocks": [{"kind": "cover"}],
            "blocks_version": BLOCKS_VERSION,
        },
    )

    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": None},
    )

    assert response.status_code == 200
    await db_session.refresh(stored_report)
    assert stored_report.design is not None
    assert "blocks" not in stored_report.design
    assert "blocks_version" not in stored_report.design
    assert stored_report.design["template"] == "chasopys"


async def test_sending_only_template_leaves_the_block_list_intact(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": [{"kind": "cover"}], "blocks_version": BLOCKS_VERSION},
    )

    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"template": "chasopys"},
    )

    assert response.status_code == 200
    await db_session.refresh(stored_report)
    assert stored_report.design is not None
    assert stored_report.design["template"] == "chasopys"
    assert [block["kind"] for block in stored_report.design["blocks"]] == ["cover"]


async def test_template_null_clears_it_alone_and_clearing_everything_nulls_the_column(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    """The fourth combination of the partial-update matrix: ``template: null``.

    Clearing one key must not disturb the other — the block list stays put. Then
    clearing both must leave ``design`` as SQL NULL, not an empty dict; a stored
    ``{}`` would read back as "a design was chosen, and it chose nothing" instead
    of "no design was ever chosen."
    """
    await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "template": "chasopys",
            "blocks": [{"kind": "cover"}],
            "blocks_version": BLOCKS_VERSION,
        },
    )

    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"template": None},
    )

    assert response.status_code == 200
    assert response.json()["template"] is None
    await db_session.refresh(stored_report)
    assert stored_report.design is not None
    assert "template" not in stored_report.design
    assert [block["kind"] for block in stored_report.design["blocks"]] == ["cover"]

    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": None},
    )

    assert response.status_code == 200
    await db_session.refresh(stored_report)
    assert stored_report.design is None


@pytest.fixture
async def signed_in_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Any authenticated ordinary user — no repository, no grant, no report.

    That absence is the point: the template list is repository-independent, so a
    fixture carrying a repository would hide it if the route ever grew one. Built
    as its own client, like ``other_user_client``, so the bare ``client`` fixture
    stays unauthenticated for the 401 test below.
    """
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        await _user(db_session, Role.USER, "template-reader")
        token = await _token(c, "template-reader")
        c.headers.update(_auth(token))
        yield c


async def test_the_template_list_offers_every_shipped_template(
    signed_in_client: AsyncClient,
) -> None:
    response = await signed_in_client.get("/report-templates")

    assert response.status_code == 200
    assert {choice["key"] for choice in response.json()} == set(TEMPLATES)


async def test_the_default_template_is_listed_first(signed_in_client: AsyncClient) -> None:
    """The reader's eye lands on the one they get if they choose nothing."""
    response = await signed_in_client.get("/report-templates")

    assert response.json()[0]["key"] == resolve_style(None).key


async def test_each_choice_carries_the_swatch_a_ui_needs(signed_in_client: AsyncClient) -> None:
    """Hexes come off ``ReportStyle`` so no UI hardcodes a palette that module owns."""
    response = await signed_in_client.get("/report-templates")

    prezydia = next(c for c in response.json() if c["key"] == "prezydia")
    assert prezydia["name"] == "Президія"
    assert prezydia["paper"] == TEMPLATES["prezydia"].paper
    assert prezydia["ink"] == TEMPLATES["prezydia"].ink
    assert prezydia["accent"] == TEMPLATES["prezydia"].accent


async def test_the_template_list_needs_a_signed_in_user(client: AsyncClient) -> None:
    response = await client.get("/report-templates")

    assert response.status_code == 401


# --------------------------------------------------------------------------- #
# Reading a design back: GET .../design (step 5d)
# --------------------------------------------------------------------------- #


async def test_the_design_of_an_unedited_report_is_the_derived_default(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The editor opens on the list the report *actually renders*, not on an empty
    tree it would then have to invent — and says the list is derived, so a UI can
    tell "unedited" from "edited into exactly the default"."""
    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stored"] is False
    assert body["blocks_version"] == BLOCKS_VERSION
    assert [block["kind"] for block in body["blocks"]][:2] == ["cover", "heading"]


async def test_the_derived_default_carries_the_chart_block_storage_cannot_know_about(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The trap this endpoint exists to avoid. ``chart_svg`` is never persisted, so
    deriving straight from ``load_sections`` yields a tree with no figure in it —
    while the sheet beside it plainly shows one."""
    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    blocks = response.json()["blocks"]
    assert {"kind": "viz", "section": 0, "variant": "chart"} in blocks
    assert response.json()["sections"][0]["draws_chart"] is True


async def test_a_stored_block_list_is_returned_verbatim(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    stored = [{"kind": "cover"}, {"kind": "heading", "section": 0}]
    await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"blocks": stored, "blocks_version": BLOCKS_VERSION},
    )

    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    assert response.json()["stored"] is True
    assert response.json()["blocks"] == stored


async def test_the_design_names_which_chart_types_each_section_permits(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The variant chips are driven by this, not by a hardcoded list in the SPA —
    which is what stops the editor offering a pie for a time series (parent §3)."""
    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    section = response.json()["sections"][0]
    assert section["index"] == 0
    assert section["title"] == "Виторг"
    assert section["kind"] == "data"
    assert set(section["allowed_chart_types"]) == set(allowed_chart_types(_one_data_section()))


async def test_a_time_series_section_is_not_offered_a_pie(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    """The assertion above cannot fail on its own, and that was measured rather
    than assumed: ``_one_data_section`` is a single row on a column named ``fy``,
    which legitimately permits all three types — so replacing the endpoint's
    ``allowed_chart_types`` call with a hardcoded ``["bar", "line", "pie"]`` left
    every test green. **A mutation that does not bite means the test is too
    coarse**, so this one gives the report a section that must be refused.

    ``fiscal_year``, not ``fy``: ``is_period_column`` recognises the written-out
    name and not the abbreviation, which is what decides whether the pie guard
    ever runs. (A column literally named ``period`` holding bare years is not
    recognised either — a gap in #131's naming rules, pre-existing and out of
    scope here, but real.)
    """
    section = SectionResult(
        title="Виторг за роками",
        source="database",
        sub_question="Скільки?",
        kind="data",
        result=QueryResult(
            columns=["fiscal_year", "revenue"],
            rows=[
                (2022, Decimal("394.3")),
                (2023, Decimal("383.3")),
                (2024, Decimal("391.0")),
            ],
        ),
        chart_spec={"chart_type": "bar", "x_column": "fiscal_year", "y_column": "revenue"},
    )
    _store_sections(stored_report, title="Звіт", language="uk", sections=[section])
    await db_session.commit()

    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    offered = response.json()["sections"][0]["allowed_chart_types"]
    assert offered == ["bar", "line"]
    assert "pie" not in offered


async def test_a_report_with_no_section_data_has_an_empty_design_rather_than_an_error(
    client: AsyncClient, legacy_report: GeneratedReport
) -> None:
    """A GET stays safe. ``can_rerender`` is already how the UI learns this report
    cannot be edited; a second, differently-shaped refusal here would only give it
    a second thing to handle."""
    response = await client.get(
        f"/repositories/{legacy_report.repository_id}/reports/{legacy_report.id}/design"
    )

    assert response.status_code == 200
    assert response.json()["blocks"] == []
    assert response.json()["sections"] == []


async def test_a_stranger_cannot_read_a_report_design(
    other_user_client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await other_user_client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    assert response.status_code == 404


async def test_a_hidden_block_is_stored_and_read_back(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "blocks": [{"kind": "cover"}, {"kind": "stats", "section": 0, "hidden": True}],
            "blocks_version": BLOCKS_VERSION,
        },
    )

    await db_session.refresh(stored_report)
    assert stored_report.design is not None
    assert stored_report.design["blocks"][1]["hidden"] is True
    # A visible block stores no flag at all: the payload records departures from
    # the default, so adding a field does not rewrite every block already stored.
    assert "hidden" not in stored_report.design["blocks"][0]


# --------------------------------------------------------------------------- #
# Editing the generated text: overrides on the design (step 5e)
# --------------------------------------------------------------------------- #


async def test_the_design_carries_the_words_each_block_would_otherwise_render(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    """The editor seeds its fields from this. Without it the only way to show the
    reader what a block currently says would be to parse it back out of the PDF."""
    section = SectionResult(
        title="Що показують дані",
        source="documents",
        sub_question="?",
        kind="text",
        prose="Дохід зріс на 12% [1].",
        sources=[SectionSourceRef(number=1, title="Звіт 2024", source_id=uuid.uuid4())],
    )
    _store_sections(stored_report, title="Річний звіт", language="uk", sections=[section])
    await db_session.commit()

    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    body = response.json()
    assert body["title"] == "Річний звіт"
    assert body["sections"][0]["prose"] == "Дохід зріс на 12% [1]."
    assert body["sections"][0]["sources"] == "[1] Звіт 2024"


async def test_an_override_is_stored_and_read_back_byte_identically(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The citation round-trip, at the layer that could break it: ``[1]`` is
    literal text in the payload, and store → load must not re-encode it."""
    prose = "Виторг зріс на 12% [1], а маржа впала [2]."
    url = f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    blocks = [{"kind": "heading", "section": 0}, {"kind": "prose", "section": 0, "text": prose}]

    stored = await client.put(url, json={"blocks": blocks, "blocks_version": BLOCKS_VERSION})
    assert stored.status_code == 200

    read_back = await client.get(url)
    assert read_back.json()["blocks"] == blocks
    assert read_back.json()["blocks"][1]["text"] == prose


async def test_text_on_a_computed_block_is_refused_by_the_endpoint(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The parent design's non-goal is a 422, not a control the UI declines to
    draw: this endpoint takes a block list the client composes."""
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "blocks": [{"kind": "stats", "section": 0, "text": "Σ Виторг: 999"}],
            "blocks_version": BLOCKS_VERSION,
        },
    )

    assert response.status_code == 422
    assert "text" in response.json()["detail"]


async def test_an_override_reaches_the_rebuilt_document(
    client: AsyncClient,
    db_session: AsyncSession,
    stored_report: GeneratedReport,
    rerender_calls: list[uuid.UUID],
) -> None:
    """Stored is not rendered. The design is read by the rebuild, so an edit that
    round-trips the endpoint but never reaches ``build_report_html`` would pass
    every assertion above and print the model's words anyway."""
    url = f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    await client.put(
        url,
        json={
            "blocks": [
                {"kind": "cover", "text": "Квартальний звіт"},
                {"kind": "heading", "section": 0, "text": "Мій заголовок"},
            ],
            "blocks_version": BLOCKS_VERSION,
        },
    )
    await db_session.refresh(stored_report)

    assert stored_report.sections_data is not None
    _, _, sections = load_sections(stored_report.sections_data)
    html = build_report_html(
        title="Звіт",
        sections=sections,
        language="uk",
        blocks=load_blocks(stored_report.design),
    )

    assert "<h1>Квартальний звіт</h1>" in html
    assert "<h2>Мій заголовок</h2>" in html
    assert "Виторг" not in html


async def test_a_regenerated_report_does_not_inherit_another_report_s_edits(
    client: AsyncClient, db_session: AsyncSession, stored_report: GeneratedReport
) -> None:
    """The documented re-generation behaviour, tested rather than assumed.

    An edit is an override on *this row's* design. Re-generating produces a new
    row, which inherits only the template from the repository default — so last
    quarter's sentence is never pasted over this quarter's numbers.
    """
    await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "blocks": [{"kind": "prose", "section": 0, "text": "Мій абзац."}],
            "blocks_version": BLOCKS_VERSION,
        },
    )
    await db_session.refresh(stored_report)

    fresh = GeneratedReport(
        repository_id=stored_report.repository_id,
        requested_by=stored_report.requested_by,
        prompt="Ще раз",
        status=ReportStatus.PENDING,
    )
    db_session.add(fresh)
    await db_session.commit()

    assert load_blocks(stored_report.design) is not None
    assert load_blocks(fresh.design) is None


# --------------------------------------------------------------------------- #
# 5f — typography, page and brand groups on PUT .../design
# --------------------------------------------------------------------------- #


async def test_the_three_style_groups_are_stored_and_returned(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={
            "typography": {"scale": 1.2, "density": "airy"},
            "page": {"section_rules": True},
            "brand": {"cover": "band", "approval": True},
        },
    )

    assert response.status_code == 200, response.text
    read_back = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )
    stored = read_back.json()
    assert stored["typography"] == {"scale": 1.2, "density": "airy"}
    assert stored["page"] == {"section_rules": True}
    assert stored["brand"]["cover"] == "band"


async def test_a_group_sent_as_null_is_cleared(
    client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """Partial update, same contract the template and block list already have."""
    url = f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    await client.put(url, json={"typography": {"scale": 1.2}})
    cleared = await client.put(url, json={"typography": None})

    assert cleared.status_code == 200
    read_back = await client.get(url)
    assert read_back.json()["typography"] == {}


async def test_a_logo_from_another_repository_is_refused(
    db_session: AsyncSession, client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The access-control half of this endpoint.

    ``PUT …/design`` takes a payload the client composes, so a picker that only
    lists your own assets restricts nothing. Without this gate a crafted call
    prints another repository's bytes on this report's cover.
    """
    other = Repository(name="Інший репозиторій", description="test")
    db_session.add(other)
    await db_session.flush()
    foreign = ReportAsset(
        repository_id=other.id,
        mime="image/png",
        filename="logo.png",
        width=8,
        height=8,
        byte_size=4,
        data=b"\x89PNG",
    )
    db_session.add(foreign)
    await db_session.commit()

    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"brand": {"logo_asset_id": str(foreign.id)}},
    )

    assert response.status_code == 422
    assert str(foreign.id) in response.json()["detail"]


async def test_a_logo_from_this_repository_is_accepted(
    db_session: AsyncSession, client: AsyncClient, stored_report: GeneratedReport
) -> None:
    own = ReportAsset(
        repository_id=stored_report.repository_id,
        mime="image/png",
        filename="logo.png",
        width=8,
        height=8,
        byte_size=4,
        data=b"\x89PNG",
    )
    db_session.add(own)
    await db_session.commit()

    response = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"brand": {"logo_asset_id": str(own.id)}},
    )

    assert response.status_code == 200, response.text
    read_back = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )
    assert read_back.json()["brand"]["logo_asset_id"] == str(own.id)


# --- the design canvas's document ---------------------------------------------


@pytest.fixture
def preview_calls() -> list[uuid.UUID]:
    """Report ids the fake preview was invoked with — the route's contract is
    that it calls the builder, and a bare assert inside a fake that never runs
    proves nothing."""
    return []


@pytest.fixture
def fake_preview(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    preview_calls: list[uuid.UUID],
) -> None:
    """``preview_report_html`` acting on the shared session, for the same reason
    ``fake_rerender`` does: the real one opens its own connection through
    ``SessionLocal``, which cannot see this test's uncommitted row. The real
    builder is covered against a real session in ``test_reports_service.py``."""

    async def _preview(report_id: uuid.UUID) -> str:
        preview_calls.append(report_id)
        target = await db_session.get(GeneratedReport, report_id)
        assert target is not None
        if target.sections_data is None:
            raise ReportNotRenderable(str(report_id))
        return '<!doctype html><html><body><div class="cv-sheet">…</div></body></html>'

    monkeypatch.setattr(reports_api, "preview_report_html", _preview)


async def test_the_preview_route_returns_html_the_canvas_can_mount(
    client: AsyncClient,
    stored_report: GeneratedReport,
    fake_preview: None,
    preview_calls: list[uuid.UUID],
) -> None:
    response = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design/preview"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "cv-sheet" in response.text
    assert preview_calls == [stored_report.id]


async def test_the_preview_route_does_not_shadow_the_design_route(
    client: AsyncClient, stored_report: GeneratedReport, fake_preview: None
) -> None:
    """``/design`` and ``/design/preview`` are siblings, and FastAPI matches in
    declaration order — a route declared the other way round would swallow the
    literal path into ``{report_id}``-style matching and answer JSON here."""
    design = await client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design"
    )

    assert design.status_code == 200
    assert design.headers["content-type"].startswith("application/json")


async def test_a_report_with_no_stored_sections_cannot_be_previewed(
    client: AsyncClient, legacy_report: GeneratedReport, fake_preview: None
) -> None:
    response = await client.get(
        f"/repositories/{legacy_report.repository_id}/reports/{legacy_report.id}/design/preview"
    )

    assert response.status_code == 409


async def test_another_users_report_cannot_be_previewed(
    other_user_client: AsyncClient, stored_report: GeneratedReport, fake_preview: None
) -> None:
    response = await other_user_client.get(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design/preview"
    )

    assert response.status_code == 404


async def test_the_preview_needs_a_session(
    stored_report: GeneratedReport, fake_preview: None, db_session: AsyncSession
) -> None:
    """Unlike the font route, the document is a customer's data."""
    app = create_app()

    async def _use_test_session() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _use_test_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anonymous:
        response = await anonymous.get(
            f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design/preview"
        )

    assert response.status_code == 401


# --- the logo seam, run as one chain (#159) -----------------------------------


def _png_bytes(color: tuple[int, int, int] = (200, 30, 60)) -> bytes:
    """A real 24×12 PNG. The other logo tests store ``b"\\x89PNG"``, which is what
    let this seam stay open: four bytes pass a write gate and are not an image."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (24, 12), color).save(buffer, format="PNG")
    return buffer.getvalue()


async def test_an_uploaded_logo_travels_all_the_way_onto_the_sheet(
    db_session: AsyncSession, client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """Upload → store → bind → render, as one chain.

    Every link had a test and the chain had none (#159), which is exactly how a
    link can pass while the chain does not: the other logo tests store four bytes
    that are not an image, and nothing ever asked a renderer to draw them.

    Only the session factory is substituted — the upload goes through the real
    multipart route, the binding through the real write gate, and the document
    through the real builder.
    """
    from contextlib import asynccontextmanager

    from contextvault.services.reports import preview_report_html

    @asynccontextmanager
    async def factory() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    upload = await client.post(
        f"/repositories/{stored_report.repository_id}/report-assets",
        files={"file": ("logo.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 201, upload.text
    asset_id = upload.json()["id"]

    bound = await client.put(
        f"/repositories/{stored_report.repository_id}/reports/{stored_report.id}/design",
        json={"brand": {"logo_asset_id": asset_id}},
    )
    assert bound.status_code == 200, bound.text

    html = await preview_report_html(stored_report.id, session_factory=factory)

    # The bytes themselves, base64, in the document — not merely "it did not raise".
    # WeasyPrint drops an image it cannot parse and renders everything else, so a
    # complete-looking, logo-less cover is the failure this asserts against.
    assert '<img class="logo" src="data:image/png;base64,' in html
    assert base64.b64encode(_png_bytes()).decode() in html


async def test_a_report_with_no_logo_still_draws_a_cover(
    db_session: AsyncSession, client: AsyncClient, stored_report: GeneratedReport
) -> None:
    """The other half: the chain is opt-in, and the default cover is untouched."""
    from contextlib import asynccontextmanager

    from contextvault.services.reports import preview_report_html

    @asynccontextmanager
    async def factory() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    html = await preview_report_html(stored_report.id, session_factory=factory)

    assert '<div class="cover"' in html
    assert 'class="logo"' not in html


class TestSectionRoutingIsServerChecked:
    """#203: the server decides which databases a plan may name.

    ``/replan`` and ``/approve`` both take the plan as it stands on the caller's
    screen, so both are untrusted input. The client mirrors the rule as a display
    convenience only.
    """

    async def _awaiting(
        self, db_session: AsyncSession, admin: User, repo: Repository, conn: DatabaseConnection
    ) -> GeneratedReport:
        report = GeneratedReport(
            repository_id=repo.id,
            connection_id=conn.id,
            requested_by=admin.id,
            prompt="p",
            status=ReportStatus.AWAITING_APPROVAL,
            plan=_draft(conn),
            plan_rounds=1,
        )
        db_session.add(report)
        await db_session.commit()
        return report

    async def test_approve_refuses_another_repositorys_database(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The routing is a connection id in a JSON body a caller controls. Without
        this check it would reach ``generate_report``, which resolves a connection
        and runs SQL with its stored credentials — a repository boundary crossed by
        editing one field."""
        admin = await _user(db_session, Role.ADMIN, "admin-routing-1")
        mine = await _repo(db_session)
        my_conn = await _connection(db_session, mine.id)
        theirs = await _repo(db_session)
        their_conn = await _connection(db_session, theirs.id)
        report = await self._awaiting(db_session, admin, mine, my_conn)
        monkeypatch.setattr(reports_api, "run_report_generation", _make_noop_fake([]))

        resp = await client.post(
            f"/repositories/{mine.id}/reports/{report.id}/approve",
            json=_draft(their_conn),
            headers=_auth(await _token(client, admin.username)),
        )
        assert resp.status_code == 400
        assert "does not belong to this repository" in resp.json()["detail"]

    async def test_approve_refuses_more_databases_than_a_section_may_merge(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#204 raised the ceiling; it did not remove it. Each named database is
        a full self-repair loop, so an unbounded list is a caller-controlled bill."""
        admin = await _user(db_session, Role.ADMIN, "admin-routing-merge-1")
        repo = await _repo(db_session)
        conns = [await _connection(db_session, repo.id, name=f"db-{i}") for i in range(4)]
        report = await self._awaiting(db_session, admin, repo, conns[0])
        monkeypatch.setattr(reports_api, "run_report_generation", _make_noop_fake([]))
        body = _draft(conns[0])
        body["sections"][0]["databases"] = [str(c.id) for c in conns]
        body["sections"][0]["columns"] = ["company", "revenue"]

        resp = await client.post(
            f"/repositories/{repo.id}/reports/{report.id}/approve",
            json=body,
            headers=_auth(await _token(client, admin.username)),
        )
        assert resp.status_code == 400
        assert "at most 3 databases" in resp.json()["detail"]

    async def test_approve_refuses_a_merged_section_with_no_output_contract(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Approval is the last gate before generation spends provider calls, and
        without a contract the two results cannot be put in one table at all."""
        admin = await _user(db_session, Role.ADMIN, "admin-routing-merge-2")
        repo = await _repo(db_session)
        first = await _connection(db_session, repo.id, name="db-a")
        second = await _connection(db_session, repo.id, name="db-b")
        report = await self._awaiting(db_session, admin, repo, first)
        monkeypatch.setattr(reports_api, "run_report_generation", _make_noop_fake([]))
        body = _draft(first)
        body["sections"][0]["databases"] = [str(first.id), str(second.id)]

        resp = await client.post(
            f"/repositories/{repo.id}/reports/{report.id}/approve",
            json=body,
            headers=_auth(await _token(client, admin.username)),
        )
        assert resp.status_code == 400
        assert "must list the columns" in resp.json()["detail"]

    async def test_replan_refuses_another_repositorys_database(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same check on the other untrusted path — rejected before a provider
        call is spent on a plan that could never be approved."""
        admin = await _user(db_session, Role.ADMIN, "admin-routing-2")
        mine = await _repo(db_session)
        my_conn = await _connection(db_session, mine.id)
        theirs = await _repo(db_session)
        their_conn = await _connection(db_session, theirs.id)
        report = await self._awaiting(db_session, admin, mine, my_conn)

        # Signature-matched to the real entrypoint (report_id, draft): a
        # single-argument fake would raise TypeError instead of recording a
        # dispatch, so the "no round was spent" assertion below could never fail
        # for the reason it claims to check.
        calls: list[uuid.UUID] = []

        async def fake_replanning(report_id: uuid.UUID, draft_data: dict[str, Any]) -> None:
            calls.append(report_id)

        monkeypatch.setattr(reports_api, "run_report_replanning", fake_replanning)

        resp = await client.post(
            f"/repositories/{mine.id}/reports/{report.id}/replan",
            json=_draft(their_conn),
            headers=_auth(await _token(client, admin.username)),
        )
        assert resp.status_code == 400
        assert calls == []  # no round was dispatched

    async def test_approve_accepts_a_second_database_of_its_own_repository(
        self, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the guard: the check is about ownership, not about
        being *the* connection the report was created with — a repository has
        several since #201 and any of them is routable."""
        admin = await _user(db_session, Role.ADMIN, "admin-routing-3")
        repo = await _repo(db_session)
        first = await _connection(db_session, repo.id, name="Apple")
        second = await _connection(db_session, repo.id, name="Microsoft")
        report = await self._awaiting(db_session, admin, repo, first)

        calls: list[uuid.UUID] = []
        monkeypatch.setattr(reports_api, "run_report_generation", _make_noop_fake(calls))

        resp = await client.post(
            f"/repositories/{repo.id}/reports/{report.id}/approve",
            json=_draft(second),
            headers=_auth(await _token(client, admin.username)),
        )
        assert resp.status_code == 200
        assert calls == [report.id]


class TestReportDatabasesEndpoint:
    """#203: the plan editor needs names, and its audience is not admins."""

    async def test_a_granted_user_can_read_the_database_names(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """`GET .../databases` is admin-only, so a reader reviewing a plan could
        not resolve the id a section is routed to. Routing they cannot read is
        routing they cannot review."""
        repo = await _repo(db_session)
        await _connection(db_session, repo.id, name="Microsoft")
        await _connection(db_session, repo.id, name="Apple")
        user = await _user(db_session, Role.USER, "reader-db")
        await _grant(db_session, user.id, repo.id)

        resp = await client.get(
            f"/repositories/{repo.id}/report-databases",
            headers=_auth(await _token(client, "reader-db")),
        )
        assert resp.status_code == 200
        assert [d["name"] for d in resp.json()] == ["Apple", "Microsoft"]  # by name

    async def test_it_never_returns_connection_detail(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        """A reader gets a name, not a way to reach the database."""
        repo = await _repo(db_session)
        await _connection(db_session, repo.id)
        user = await _user(db_session, Role.USER, "reader-db-2")
        await _grant(db_session, user.id, repo.id)

        resp = await client.get(
            f"/repositories/{repo.id}/report-databases",
            headers=_auth(await _token(client, "reader-db-2")),
        )
        assert resp.status_code == 200
        assert set(resp.json()[0]) == {"id", "name", "description"}

    async def test_an_ungranted_user_is_refused(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        repo = await _repo(db_session)
        await _connection(db_session, repo.id)
        await _user(db_session, Role.USER, "stranger-db")  # no grant

        resp = await client.get(
            f"/repositories/{repo.id}/report-databases",
            headers=_auth(await _token(client, "stranger-db")),
        )
        assert resp.status_code == 403
