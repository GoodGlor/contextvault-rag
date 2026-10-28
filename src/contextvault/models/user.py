"""User account model."""

from sqlalchemy import Boolean, Enum, String, text
from sqlalchemy.orm import Mapped, mapped_column

from contextvault.db.base import Base
from contextvault.models.enums import Role
from contextvault.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An account that can authenticate. Role decides admin vs user privileges."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[Role] = mapped_column(
        Enum(Role, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=Role.USER,
    )
    # Forces a password change on next login (temp-password recovery flow).
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
