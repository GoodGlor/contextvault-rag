"""Invite tokens — single-use onboarding secrets, stored only as a hash.

An invite token is a high-entropy random secret shown to the invited user exactly
once (in the invite link). Only its SHA-256 digest is persisted, so a leaked
database never yields a usable invite — acceptance re-hashes the presented token
and matches it against the stored digest.

Unlike passwords (``core/security.py`` uses Argon2), invite tokens carry full
cryptographic entropy, so a fast digest is both sufficient and appropriate here:
there is nothing to brute-force. Unlike provider keys (``core/crypto.py`` uses
reversible Fernet), an invite token never needs to be recovered — only compared —
so it is hashed one-way, not encrypted.
"""

import hashlib
import secrets

# Bytes of entropy in a raw token before url-safe encoding. 32 bytes (256 bits)
# is well beyond guessing range; token_urlsafe expands this to ~43 characters.
_TOKEN_BYTES = 32


def generate_invite_token() -> tuple[str, str]:
    """Return a fresh ``(raw_token, token_hash)`` pair.

    The raw token is handed to the user once and never stored; the hash is what
    the invitation row keeps so the token can be matched at acceptance.
    """
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    return raw, hash_invite_token(raw)


def hash_invite_token(raw: str) -> str:
    """Return the stored digest for a raw invite token (hex SHA-256)."""
    return hashlib.sha256(raw.encode()).hexdigest()
