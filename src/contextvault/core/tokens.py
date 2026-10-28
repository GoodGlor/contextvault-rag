"""JWT access tokens for authenticated sessions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from contextvault.core.config import get_settings


class InvalidToken(Exception):
    """Raised when a token is missing, malformed, expired, or badly signed."""


@dataclass(frozen=True)
class TokenClaims:
    """The application-level claims we care about, extracted from a valid token."""

    subject: str
    role: str


def create_access_token(*, subject: str, role: str, expires_delta: timedelta | None = None) -> str:
    """Sign a JWT whose ``sub`` is the user id and which carries the user's role."""
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "role": role,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenClaims:
    """Verify a token's signature and expiry and return its claims.

    Raises :class:`InvalidToken` for any problem so callers never see a leaky
    library-specific exception.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise InvalidToken(str(exc)) from exc
    subject = payload.get("sub")
    role = payload.get("role")
    if not isinstance(subject, str) or not isinstance(role, str):
        raise InvalidToken("token missing required claims")
    return TokenClaims(subject=subject, role=role)
