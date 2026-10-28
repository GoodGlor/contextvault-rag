"""Password hashing with Argon2.

A single shared ``PasswordHasher`` carries the tuning parameters; all passwords
(including temporary recovery passwords) go through here.
"""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

_hasher = PasswordHasher()

# Bytes of entropy in a generated temporary password before url-safe encoding.
_TEMP_PASSWORD_BYTES = 12


def hash_password(password: str) -> str:
    """Return an Argon2 hash (with embedded salt and parameters) for a password."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True iff ``password`` matches ``password_hash``.

    Returns False for a wrong password or a malformed/foreign hash rather than
    raising, so callers can treat it as a plain boolean check.
    """
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, ValueError):
        return False


def generate_temporary_password() -> str:
    """Return a random, url-safe temporary password for admin-issued recovery.

    Shown to the admin once and then hashed like any other password; the user is
    forced to replace it on next login (``must_change_password``).
    """
    return secrets.token_urlsafe(_TEMP_PASSWORD_BYTES)
