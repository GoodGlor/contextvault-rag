"""Uploaded images a report can print: a logo, and the pictures of ``image`` blocks.

Bytes live in Postgres, like ``pdf_data`` and ``pptx_data``, rather than in an
object store: this app has no bucket, no credentials for one, and adding a
network hop to the render path would buy a second failure mode for a file that
is capped at 2 MB. The mockup states the same intent to the reader — the file is
embedded in the report rather than linked, so it survives a rebuild.

An asset belongs to a **repository**, not to a report: the same logo is worn by
many reports, so deleting a report must not take it. ``design.brand.logo_asset_id``
and an ``image`` block's ``asset_id`` reference it by id, which is what keeps a
300 ms debounced ``PUT …/design`` from shipping the bytes on every keystroke.
"""

import uuid

from sqlalchemy import ForeignKey, Integer, LargeBinary, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class ReportAsset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "report_assets"

    repository_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("repositories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    mime: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Stored rather than derived: the deck needs the aspect ratio at
    # ``add_picture`` time, and decoding the bytes on every render to learn it
    # would be waste.
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
