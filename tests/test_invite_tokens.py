"""Unit tests for invite-token generation and hashing.

An invite token is a high-entropy secret handed to the invited user once; only
its hash is ever stored, so a leaked database never yields a usable invite. These
tests pin the two guarantees the storage model relies on: the raw token is
unguessable and unique per call, and hashing is deterministic so a presented
token can be matched against the stored digest.
"""

from contextvault.core.invite_tokens import generate_invite_token, hash_invite_token


def test_generate_returns_raw_and_matching_hash() -> None:
    raw, digest = generate_invite_token()
    # The digest stored in the DB must be reproducible from the raw token the
    # user presents — that is how acceptance looks an invite up.
    assert digest == hash_invite_token(raw)
    # The hash is not the raw token (we never store the secret itself).
    assert digest != raw


def test_raw_token_is_high_entropy() -> None:
    raw, _ = generate_invite_token()
    # A url-safe token long enough to resist guessing (>=32 bytes of entropy).
    assert len(raw) >= 32
    assert raw.strip() == raw


def test_tokens_are_unique_per_call() -> None:
    raws = {generate_invite_token()[0] for _ in range(50)}
    assert len(raws) == 50


def test_hash_is_deterministic_and_hex() -> None:
    digest_a = hash_invite_token("some-token")
    digest_b = hash_invite_token("some-token")
    assert digest_a == digest_b
    # Stored as a fixed-width hex SHA-256 digest.
    assert len(digest_a) == 64
    int(digest_a, 16)  # raises if not hex
