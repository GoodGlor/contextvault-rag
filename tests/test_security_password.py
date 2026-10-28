"""Tests for Argon2 password hashing."""

from contextvault.core.security import hash_password, verify_password


def test_hash_is_argon2_and_not_plaintext() -> None:
    digest = hash_password("correct horse battery staple")
    assert digest != "correct horse battery staple"
    assert digest.startswith("$argon2")


def test_verify_accepts_correct_password() -> None:
    digest = hash_password("s3cret")
    assert verify_password("s3cret", digest) is True


def test_verify_rejects_wrong_password() -> None:
    digest = hash_password("s3cret")
    assert verify_password("guess", digest) is False


def test_verify_rejects_malformed_hash() -> None:
    assert verify_password("s3cret", "not-a-hash") is False


def test_hashing_is_salted() -> None:
    assert hash_password("same") != hash_password("same")
