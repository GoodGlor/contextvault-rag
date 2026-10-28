"""Generated reports and their nightly schedules."""

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.enums import ReportStatus
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ReportSchedule(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A frozen, validated report query re-executed nightly for its owner.

    ``frozen_sql``/``frozen_chart_spec`` are the already-guardrailed artifacts of
    the report the schedule was created from — nightly runs re-execute them
    verbatim (no LLM call). Relative windows stay rolling because generation
    expresses them as SQL date arithmetic.
    """

    __tablename__ = "report_schedules"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("database_connections.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_sql: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_chart_spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    run_at_time: Mapped[datetime.time] = mapped_column(Time, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_run_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class GeneratedReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One report request and its artifact; the per-user history row.

    ``generated_sql`` is the audit trail — the exact validated SQL that ran
    (admin-visible). ``pdf_data`` holds the artifact bytes (reports are small;
    no blob storage exists in this app by design).
    """

    __tablename__ = "generated_reports"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("database_connections.id", ondelete="CASCADE"), nullable=True
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    generated_sql: Mapped[str | None] = mapped_column(Text, nullable=True)
    chart_spec: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Blended-report audit trail: the ordered plan + per-section outcome
    # (admin-visible, the analogue of ``generated_sql`` for multi-section reports).
    sections: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    # The executed sections themselves: columns, rows, prose, citations. Separate
    # from ``sections`` above, which is a short audit a human reads — merging them
    # would put megabytes of result rows behind an audit view. Null means the
    # report predates this column, or its payload exceeded the size budget; either
    # way it cannot be re-rendered.
    sections_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Which template this report wears, snapshotted at generation from the
    # repository's default. Re-render reads *this* and never the repository: a
    # default that moves must not restyle a report that is already finished.
    # Null means the report predates this column and renders in the default.
    # Step 4 grows this payload a block list; the shape is additive on purpose.
    design: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The plan under review, then the approved plan generation executes. Written by
    # planning and by every re-plan; null for frozen-schedule reports, which have no
    # plan, and for reports created before the review loop existed.
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # How many LLM planning rounds this report cost. A re-plan with nothing left
    # blank makes no provider call and does not increment it.
    plan_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus, name="report_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=ReportStatus.PENDING,
        server_default=ReportStatus.PENDING.value,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pdf_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Both artifacts are built once at generation time so a download is a plain
    # byte read — the reader chooses the format, not the generator.
    pptx_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pptx_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("report_schedules.id", ondelete="SET NULL"), nullable=True
    )
