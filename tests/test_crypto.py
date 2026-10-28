"""Tests for symmetric encryption of provider API keys (encrypt at rest)."""

from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from contextvault.core import crypto
from contextvault.core.crypto import EncryptionError, decrypt, encrypt, mask_key

_SECRET = "sk-proj-abcdefghijklmnop4f2a"


def test_round_trip_recovers_plaintext() -> None:
    assert decrypt(encrypt(_SECRET)) == _SECRET


def test_ciphertext_hides_plaintext() -> None:
    token = encrypt(_SECRET)
    assert _SECRET not in token
    assert token != _SECRET


def test_encrypt_is_nondeterministic() -> None:
    # Fernet embeds a random IV, so the same secret encrypts to a fresh token
    # each time — ciphertext never leaks that two repos share a key.
    assert encrypt(_SECRET) != encrypt(_SECRET)


def test_decrypt_rejects_tampered_ciphertext() -> None:
    token = encrypt(_SECRET)
    tampered = token[:-3] + ("XYZ" if token[-3:] != "XYZ" else "ABC")
    with pytest.raises(EncryptionError):
        decrypt(tampered)


def test_decrypt_rejects_wrong_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encrypt(_SECRET)
    other = Fernet.generate_key().decode()
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(encryption_key=other))
    with pytest.raises(EncryptionError):
        decrypt(token)


def test_encrypt_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(encryption_key=None))
    with pytest.raises(EncryptionError):
        encrypt(_SECRET)


def test_invalid_master_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        crypto, "get_settings", lambda: SimpleNamespace(encryption_key="not-a-fernet-key")
    )
    with pytest.raises(EncryptionError):
        encrypt(_SECRET)


def test_mask_key_keeps_prefix_and_suffix() -> None:
    assert mask_key("sk-proj-abcdefghijklmnop4f2a") == "sk-…•••4f2a"


def test_mask_key_never_reveals_full_secret() -> None:
    key = "sk-proj-abcdefghijklmnop4f2a"
    masked = mask_key(key)
    assert key not in masked
    assert masked.endswith(key[-4:])


def test_mask_key_fully_masks_short_values() -> None:
    assert mask_key("short") == "•••"
    assert mask_key("") == "•••"
