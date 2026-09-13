"""Path normalization and validation for the memory store."""

from urllib.parse import unquote

from fastapi import HTTPException

MEMORY_ROOT = ""


def normalize_memory_path(raw: str | None, *, allow_root: bool = False) -> str:
    """Normalize and validate a client-supplied memory path.

    Rejects path traversal and backslashes. Returns MEMORY_ROOT for an
    empty/root path when allow_root is True; raises otherwise.
    """
    if raw is None:
        raw = ""

    # Decode percent-encoding first so an encoded ".." can't slip through.
    decoded = unquote(raw)

    if "\\" in decoded:
        raise HTTPException(
            status_code=400, detail=f"Error: invalid path {raw!r} — backslashes are not allowed"
        )

    normalized = decoded.strip("/")

    if not normalized:
        if allow_root:
            return MEMORY_ROOT
        raise HTTPException(status_code=400, detail="Error: path is required")

    segments = normalized.split("/")
    if any(segment in ("..", ".") or segment == "" for segment in segments):
        raise HTTPException(
            status_code=400, detail=f"Error: invalid path {raw!r} — path traversal is not allowed"
        )

    return normalized


def is_root(path: str) -> bool:
    """True if a normalized path is the memory root sentinel."""
    return path == MEMORY_ROOT
