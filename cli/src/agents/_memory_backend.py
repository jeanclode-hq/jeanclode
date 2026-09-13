"""Transport + presentation logic for the memory tool.

No dependency on claude_agent_sdk, so it stays testable independent of the
SDK. Proxies to the backend's /internal/memory/* endpoints and adds line
numbering, view_range slicing, truncation, and directory-listing formatting
— none of which the backend does itself.

Auth: never constructs an Authorization header — the security-proxy sidecar
injects it transparently, same as gh/glab.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

MEMORY_API_URL_ENV = "JEANCLODE_MEMORY_API_URL"
MAX_CONTENT_CHARS = 16_000
MAX_LINES = 999_999
_REQUEST_TIMEOUT_SECONDS = 30.0


class MemoryToolError(Exception):
    """Surfaced as a tool-level error result, never left to propagate."""


def _build_client() -> httpx.AsyncClient:
    """Thin seam for tests: patch this to use httpx.MockTransport."""
    base_url = os.environ.get(MEMORY_API_URL_ENV, "").strip().rstrip("/")
    if not base_url:
        raise MemoryToolError(
            f"Error: {MEMORY_API_URL_ENV} is not set; cannot reach the memory backend"
        )
    return httpx.AsyncClient(base_url=base_url, timeout=_REQUEST_TIMEOUT_SECONDS)


async def backend_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Issue one request to the memory backend, returning (status, json body)."""
    client = _build_client()
    async with client:
        try:
            response = await client.request(method, path, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise MemoryToolError(f"Error: could not reach memory backend: {exc}") from exc

    if not response.content:
        return response.status_code, {}
    try:
        data = response.json()
    except ValueError:
        return response.status_code, {}
    return response.status_code, data if isinstance(data, dict) else {}


def error_detail(data: dict[str, Any]) -> str:
    detail = data.get("detail")
    return str(detail) if detail else "Error: unknown error from memory backend"


def text_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def number_lines(lines: list[str], start: int) -> str:
    """6-char right-aligned, 1-indexed, tab-separated. `start` is lines[0]'s line number."""
    return "\n".join(f"{start + i:6d}\t{line}" for i, line in enumerate(lines))


def parse_view_range(raw: object) -> tuple[int, int] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        raise MemoryToolError("Error: view_range must be a list of exactly 2 integers")
    start, end = raw
    if not isinstance(start, int) or not isinstance(end, int):
        raise MemoryToolError("Error: view_range must be a list of exactly 2 integers")
    if start < 1:
        raise MemoryToolError("Error: view_range start_line must be >= 1")
    if end != -1 and end < start:
        raise MemoryToolError("Error: view_range end_line must be -1 or >= start_line")
    return start, end


def format_file_view(content: str, path: str, view_range: tuple[int, int] | None) -> str:
    """Apply the line-count ceiling, then view_range slicing, then truncation, then numbering."""
    lines = content.split("\n")
    total_lines = len(lines)
    if total_lines > MAX_LINES:
        return f"File {path} exceeds maximum line limit of {MAX_LINES:,} lines."

    start, end = 1, total_lines
    if view_range is not None:
        start, end = view_range
        end = total_lines if end == -1 else min(end, total_lines)
        if start > total_lines:
            raise MemoryToolError(
                f"Error: view_range start_line {start} is beyond the end of {path} "
                f"({total_lines} lines)"
            )

    selected = lines[start - 1 : end]
    text = "\n".join(selected)

    truncated = False
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS]
        truncated = True

    numbered = number_lines(text.split("\n"), start)
    if truncated:
        numbered += (
            f"\n\n[TRUNCATED: this view exceeds {MAX_CONTENT_CHARS:,} characters and was cut "
            "off here. Use view_range to page through the rest of the file.]"
        )
    return numbered


def human_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return str(num_bytes)
    value = float(num_bytes)
    for unit in ("K", "M", "G", "T"):
        value /= 1024
        if value < 1024 or unit == "T":
            return f"{value:.1f}{unit}"
    return f"{value:.1f}T"  # pragma: no cover — unreachable, satisfies type-checking


def format_directory_listing(path: str, entries: list[dict[str, Any]]) -> str:
    """Tab-separated `path\\tsize`; a folded directory-marker entry renders as `<DIR>`."""
    header = f"Directory: {path or '/'}"
    if not entries:
        return f"{header}\n(empty)"
    rows = [header, ""]
    for entry in entries:
        size = entry.get("size")
        size_str = human_size(size) if size is not None else "<DIR>"
        rows.append(f"{entry['path']}\t{size_str}")
    return "\n".join(rows)
