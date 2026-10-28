"""Persistence round-trips for the DB-reports models."""

import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import (
    DatabaseConnection,
    DatabaseType,
    GeneratedReport,
    ReportSchedule,
    ReportStatus,
    Repository,
)


async def test_report_models_round_trip(db_session: AsyncSession) -> None:
    repo = Repository(name="Vault")
    db_session.add(repo)
    await db_session.flush()

    conn = DatabaseConnection(
        repository_id=repo.id,
        name="Reporting",
        db_type=DatabaseType.POSTGRES,
        host="db.internal",
        port=5432,
        database="sales",
        username="reader",
        password_encrypted="gAAA-cipher",
        exposed_schema=[
            {"table": "orders", "description": "", "columns": [{"name": "city", "description": ""}]}
        ],
    )
    db_session.add(conn)
    await db_session.flush()

    schedule = ReportSchedule(
        repository_id=repo.id,
        connection_id=conn.id,
        owner_id=None,
        prompt="nightly sales",
        frozen_sql="SELECT 1",
        frozen_chart_spec={"chart_type": "none", "x_column": None, "y_column": None, "title": "t"},
        run_at_time=datetime.time(1, 0),
    )
    db_session.add(schedule)
    await db_session.flush()
    assert schedule.enabled is True
    assert schedule.last_run_at is None

    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=conn.id,
        requested_by=None,
        prompt="report for Kyiv",
        schedule_id=schedule.id,
    )
    db_session.add(report)
    await db_session.flush()
    assert report.status is ReportStatus.PENDING
    assert report.generated_sql is None
    assert report.pdf_data is None


async def test_report_without_connection_stores_sections_audit(db_session: AsyncSession) -> None:
    """A document-only report has no DB connection and carries its sections audit."""
    repo = Repository(name="Docs only vault")
    db_session.add(repo)
    await db_session.flush()

    report = GeneratedReport(
        repository_id=repo.id,
        connection_id=None,
        requested_by=None,
        prompt="огляд політики відпусток",
        sections=[
            {
                "title": "Тривалість відпустки",
                "source": "documents",
                "sub_question": "Яка тривалість відпустки?",
                "status": "done",
                "error": None,
                "generated_sql": None,
                "chart_spec": None,
                "cited_source_ids": [],
            }
        ],
    )
    db_session.add(report)
    await db_session.flush()
    await db_session.refresh(report)

    assert report.connection_id is None
    assert report.sections is not None
    assert report.sections[0]["source"] == "documents"


async def test_report_stores_plan_and_rounds(db_session: AsyncSession) -> None:
    """A report awaiting approval carries its plan and how many rounds it cost."""
    repo = Repository(name="Vault", llm_provider="gemini", llm_model="gem-1")
    db_session.add(repo)
    await db_session.flush()

    report = GeneratedReport(
        repository_id=repo.id,
        prompt="дохід за рік",
        status=ReportStatus.AWAITING_APPROVAL,
        plan={
            "title": "Дохід",
            "language": "uk",
            "sections": [
                {"title": "Динаміка", "source": "database", "sub_question": "дохід за FY25?"}
            ],
        },
        plan_rounds=2,
    )
    db_session.add(report)
    await db_session.flush()
    report_id = report.id
    db_session.expire(report)

    loaded = await db_session.get(GeneratedReport, report_id)
    assert loaded is not None
    assert loaded.status is ReportStatus.AWAITING_APPROVAL
    assert loaded.plan is not None
    assert loaded.plan["sections"][0]["source"] == "database"
    assert loaded.plan_rounds == 2


async def test_report_stores_sections_data(db_session: AsyncSession) -> None:
    """A completed report carries the full executed-section payload, nested intact."""
    repo = Repository(name="Vault", llm_provider="gemini", llm_model="gem-1")
    db_session.add(repo)
    await db_session.flush()

    report = GeneratedReport(
        repository_id=repo.id,
        prompt="дохід за рік",
        status=ReportStatus.DONE,
        sections_data={
            "version": 1,
            "title": "Дохід",
            "language": "uk",
            "sections": [
                {
                    "title": "Динаміка",
                    "source": "database",
                    "sub_question": "дохід за FY25?",
                    "kind": "table",
                    "columns": ["month", "revenue"],
                    "rows": [["2025-01", {"__t": "decimal", "v": "1000.50"}]],
                    "prose": None,
                    "sources": [],
                    "generated_sql": "SELECT month, revenue FROM sales",
                    "chart_spec": None,
                    "column_labels": {"month": "Місяць"},
                    "error": None,
                }
            ],
        },
    )
    db_session.add(report)
    await db_session.flush()
    report_id = report.id
    db_session.expire(report)

    loaded = await db_session.get(GeneratedReport, report_id)
    assert loaded is not None
    assert loaded.sections_data is not None
    assert loaded.sections_data["version"] == 1
    assert loaded.sections_data["title"] == "Дохід"
    assert loaded.sections_data["language"] == "uk"
    section = loaded.sections_data["sections"][0]
    assert section["source"] == "database"
    assert section["columns"] == ["month", "revenue"]
    assert section["rows"] == [["2025-01", {"__t": "decimal", "v": "1000.50"}]]
    assert section["column_labels"] == {"month": "Місяць"}


async def test_report_sections_data_defaults_to_none(db_session: AsyncSession) -> None:
    """A report created without an executed payload cannot be re-rendered."""
    repo = Repository(name="Vault2", llm_provider="gemini", llm_model="gem-1")
    db_session.add(repo)
    await db_session.flush()

    report = GeneratedReport(repository_id=repo.id, prompt="p", status=ReportStatus.PLANNING)
    db_session.add(report)
    await db_session.flush()
    report_id = report.id
    db_session.expire(report)

    loaded = await db_session.get(GeneratedReport, report_id)
    assert loaded is not None
    assert loaded.sections_data is None


async def test_report_plan_defaults(db_session: AsyncSession) -> None:
    """A freshly created report has no plan and no rounds behind it."""
    repo = Repository(name="Vault2", llm_provider="gemini", llm_model="gem-1")
    db_session.add(repo)
    await db_session.flush()

    report = GeneratedReport(repository_id=repo.id, prompt="p", status=ReportStatus.PLANNING)
    db_session.add(report)
    await db_session.flush()
    report_id = report.id
    db_session.expire(report)

    loaded = await db_session.get(GeneratedReport, report_id)
    assert loaded is not None
    assert loaded.plan is None
    assert loaded.plan_rounds == 0
