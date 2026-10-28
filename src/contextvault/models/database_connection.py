"""An admin-connected external SQL database a repository reports from."""

import uuid
from typing import Any

from sqlalchemy import Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.enums import DatabaseType
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class DatabaseConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Read-only connection details plus the admin's exposed-schema allow-list.

    **A repository owns many connections (#201).** It owned exactly one until
    then — ``uq_database_connections_repository_id``, "slice 1" — which made a
    second database unrepresentable rather than merely refused. Reports and
    schedules still each name a single connection; choosing several per report
    is #203/#204.

    ``name`` is what an admin picks from a list. Without it a connection is
    identified only by host and database, which is not something to choose
    between in a picker, and #202 hangs a generated description off the same
    row. It is unique **within a repository**: two teams may each keep a
    connection they both call "Financials", but one repository may not.

    The password is Fernet ciphertext (``core/crypto``) and is never returned by
    the API. ``exposed_schema`` is the guardrail allow-list AND the schema the
    LLM is shown:
    ``[{"table": str, "description": str, "columns": [{"name": str, "description": str}]}]``.
    """

    __tablename__ = "database_connections"
    __table_args__ = (
        UniqueConstraint("repository_id", "name", name="uq_database_connections_repository_name"),
    )

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # #202. Empty is "never described", which is NOT the same as out of date —
    # a connection is fully usable without one, so nothing here may be nullable
    # in a way that makes an absent description an error state.
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # The allow-list this description was written from. Stale is
    # `description_schema_hash != schema_fingerprint(exposed_schema)`, which is
    # self-healing: curate a column away and back, and the description is
    # current again. A boolean flag set on save would drift and stay wrong.
    description_schema_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )
    db_type: Mapped[DatabaseType] = mapped_column(
        Enum(DatabaseType, name="database_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_encrypted: Mapped[str] = mapped_column(String(1024), nullable=False)
    exposed_schema: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
