"""Mint and verify per-execution, workspace-scoped memory API tokens.

Self-verifying (HMAC signature + embedded expiry, no DB round trip) so
``/internal/memory/*`` calls stay fast and stateless. Format:
``<url-safe-base64 payload>.<hex hmac-sha256 signature>``, where payload
decodes to ``"<workspace_id>:<execution_id>:<expiry-epoch>"``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from datetime import timedelta
from uuid import UUID

from pydantic import BaseModel

DEFAULT_TTL = timedelta(hours=12)


class MemoryTokenError(ValueError):
    """Raised for any invalid, tampered, malformed, or expired token."""


class MemoryTokenPayload(BaseModel):
    """Decoded, signature-verified claims from a memory API token."""

    workspace_id: UUID
    execution_id: UUID


def mint_memory_token(
    *,
    workspace_id: UUID,
    execution_id: UUID,
    secret: str,
    ttl: timedelta = DEFAULT_TTL,
) -> str:
    """Mint a signed token scoped to one workspace and one execution."""
    expiry = int(time.time() + ttl.total_seconds())
    payload = f"{workspace_id}:{execution_id}:{expiry}"
    payload_b64 = _b64encode(payload.encode("utf-8"))
    signature = _sign(payload_b64, secret)
    return f"{payload_b64}.{signature}"


def verify_memory_token(token: str, *, secret: str) -> MemoryTokenPayload:
    """Verify a token's signature and expiry, returning its claims."""
    payload_b64, sep, signature = token.partition(".")
    if not sep or not payload_b64 or not signature:
        raise MemoryTokenError("malformed token")

    expected_signature = _sign(payload_b64, secret)
    if not hmac.compare_digest(signature, expected_signature):
        raise MemoryTokenError("invalid signature")

    try:
        payload = _b64decode(payload_b64).decode("utf-8")
        workspace_id_str, execution_id_str, expiry_str = payload.split(":")
        expiry = int(expiry_str)
        workspace_id = UUID(workspace_id_str)
        execution_id = UUID(execution_id_str)
    except (ValueError, UnicodeDecodeError, binascii.Error) as e:
        raise MemoryTokenError("malformed token payload") from e

    if time.time() >= expiry:
        raise MemoryTokenError("token expired")

    return MemoryTokenPayload(workspace_id=workspace_id, execution_id=execution_id)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)
