"""Tests for JWT access-token creation and decoding."""

from datetime import timedelta

import pytest

from contextvault.core.tokens import InvalidToken, create_access_token, decode_access_token


def test_round_trip_carries_subject_and_role() -> None:
    token = create_access_token(subject="user-123", role="admin")
    claims = decode_access_token(token)
    assert claims.subject == "user-123"
    assert claims.role == "admin"


def test_tampered_token_is_rejected() -> None:
    token = create_access_token(subject="user-123", role="user")
    with pytest.raises(InvalidToken):
        decode_access_token(token + "tampered")


def test_expired_token_is_rejected() -> None:
    token = create_access_token(subject="u", role="user", expires_delta=timedelta(minutes=-1))
    with pytest.raises(InvalidToken):
        decode_access_token(token)


def test_token_signed_with_other_secret_is_rejected() -> None:
    import jwt

    forged = jwt.encode(
        {"sub": "u", "role": "admin"},
        "a-different-secret-key-that-is-also-32-bytes",
        algorithm="HS256",
    )
    with pytest.raises(InvalidToken):
        decode_access_token(forged)
