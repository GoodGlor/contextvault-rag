"""Symmetric encryption for provider API keys — encrypt at rest.

Provider keys are live credentials, so they are only ever stored as ciphertext.
The master key lives in the environment (``ENCRYPTION_KEY``), never in the
database or the code; ciphertext is decrypted into memory only at call time.

Fernet (AES-128-CBC + HMAC) gives authenticated encryption with a random IV per
message, so identical secrets encrypt to different tokens and a tampered or
wrong-key ciphertext fails loudly (``EncryptionError``) instead of returning
garbage. The master key is a Fernet key generated with::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet, InvalidToken

from contextvault.core.config import get_settings


class EncryptionError(RuntimeError):
    """Raised when a value cannot be encrypted or decrypted."""


def _cipher() -> Fernet:
    """Build a Fernet cipher from the configured master key.

    Reads the key on every call (rather than caching) so a rotated key or a
    test override takes effect immediately. Raises ``EncryptionError`` — never
    falling back to plaintext — when the key is missing or malformed.
    """
    key = get_settings().encryption_key
    if not key:
        raise EncryptionError(
            "encryption_key is not configured; set ENCRYPTION_KEY to a Fernet key "
            '(generate one with `python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`).'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise EncryptionError("encryption_key is not a valid Fernet key") from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a secret into a URL-safe ciphertext token."""
    return _cipher().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Recover a secret from a ciphertext token.

    Raises ``EncryptionError`` if the token was tampered with or was produced
    under a different master key.
    """
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionError("ciphertext could not be decrypted with the configured key") from exc


def mask_key(key: str) -> str:
    """Return a display-safe preview of a secret, e.g. ``sk-…•••4f2a``.

    Keeps a short readable prefix and the last four characters so an admin can
    recognise which key is stored without ever revealing enough to reuse it.
    Values too short to preview safely are masked entirely.
    """
    if len(key) <= 8:
        return "•••"
    return f"{key[:3]}…•••{key[-4:]}"
