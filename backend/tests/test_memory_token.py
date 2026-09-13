"""Tests for the per-execution memory API token (issue #181).

Pure-Python signing/verification logic — no DB, no app context. Covers the
mint → verify round trip, and the three "reject" cases called out
explicitly in the issue: expired, tampered, and wrong-secret tokens.
"""

from datetime import timedelta
from uuid import uuid4

import pytest

from api.services.memory_token import (
    MemoryTokenError,
    mint_memory_token,
    verify_memory_token,
)

SECRET = "unit-test-signing-secret"


def test_round_trip_returns_the_minted_claims() -> None:
    workspace_id = uuid4()
    execution_id = uuid4()

    token = mint_memory_token(workspace_id=workspace_id, execution_id=execution_id, secret=SECRET)
    payload = verify_memory_token(token, secret=SECRET)

    assert payload.workspace_id == workspace_id
    assert payload.execution_id == execution_id


def test_expired_token_rejected() -> None:
    token = mint_memory_token(
        workspace_id=uuid4(),
        execution_id=uuid4(),
        secret=SECRET,
        ttl=timedelta(seconds=-1),
    )
    with pytest.raises(MemoryTokenError):
        verify_memory_token(token, secret=SECRET)


def test_not_yet_expired_token_accepted() -> None:
    """Sanity check for the expiry boundary: a token with time left is fine."""
    token = mint_memory_token(
        workspace_id=uuid4(),
        execution_id=uuid4(),
        secret=SECRET,
        ttl=timedelta(hours=1),
    )
    verify_memory_token(token, secret=SECRET)  # does not raise


def test_wrong_secret_rejected() -> None:
    token = mint_memory_token(workspace_id=uuid4(), execution_id=uuid4(), secret=SECRET)
    with pytest.raises(MemoryTokenError):
        verify_memory_token(token, secret="a-different-secret")


@pytest.mark.parametrize("flip_in", ["payload", "signature"])
def test_tampered_token_rejected(flip_in: str) -> None:
    """Flipping one character anywhere in the token — payload or signature
    half — invalidates the signature."""
    token = mint_memory_token(workspace_id=uuid4(), execution_id=uuid4(), secret=SECRET)
    payload_b64, signature = token.split(".")
    half = payload_b64 if flip_in == "payload" else signature
    tampered_char = "a" if half[-1] != "a" else "b"
    tampered_half = half[:-1] + tampered_char

    tampered_token = (
        f"{tampered_half}.{signature}" if flip_in == "payload" else f"{payload_b64}.{tampered_half}"
    )
    with pytest.raises(MemoryTokenError):
        verify_memory_token(tampered_token, secret=SECRET)


def test_cross_workspace_forgery_rejected() -> None:
    """Editing the workspace_id claim inside a valid token's payload (without
    re-signing, since the attacker doesn't have the secret) is caught by
    signature verification — a token can never be "upgraded" to a different
    workspace than the one it was minted for."""
    real_workspace_id = uuid4()
    forged_workspace_id = uuid4()
    execution_id = uuid4()

    token = mint_memory_token(
        workspace_id=real_workspace_id, execution_id=execution_id, secret=SECRET
    )
    _payload_b64, signature = token.split(".")

    forged_token = mint_memory_token(
        workspace_id=forged_workspace_id, execution_id=execution_id, secret=SECRET
    )
    forged_payload_b64, _ = forged_token.split(".")

    # Splice workspace A's signature onto workspace B's payload — this is
    # exactly the attack the signature must prevent.
    frankenstein_token = f"{forged_payload_b64}.{signature}"
    with pytest.raises(MemoryTokenError):
        verify_memory_token(frankenstein_token, secret=SECRET)


def test_malformed_token_missing_separator_rejected() -> None:
    with pytest.raises(MemoryTokenError):
        verify_memory_token("not-a-real-token", secret=SECRET)


def test_malformed_token_empty_rejected() -> None:
    with pytest.raises(MemoryTokenError):
        verify_memory_token("", secret=SECRET)


def test_minted_tokens_are_url_and_header_safe() -> None:
    """Bearer-token-safe charset: no characters that would need escaping in
    an `Authorization: Bearer <token>` header or a URL."""
    token = mint_memory_token(workspace_id=uuid4(), execution_id=uuid4(), secret=SECRET)
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
    assert set(token) <= allowed
