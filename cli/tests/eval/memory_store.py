"""In-memory stand-in for the backend's /internal/memory API, for curator evals.

Mirrors the backend's contract closely enough for the real memory tool to
drive it: 2-level directory listings, unique-substring str_replace, prefix
delete/rename, and soft delete (deleted entries are kept, never served).
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class FakeMemoryStore:
    def __init__(self, entries: dict[str, str]) -> None:
        self.live: dict[str, str] = dict(entries)
        self.deleted: dict[str, str] = {}
        self.initial: dict[str, str] = dict(entries)

    # -- assertions helpers -------------------------------------------------

    def containing(self, needle: str) -> list[str]:
        """Live paths whose content mentions ``needle`` (case-insensitive)."""
        return [p for p, c in self.live.items() if needle.lower() in c.lower()]

    def untouched(self, path: str) -> bool:
        return self.live.get(path) == self.initial.get(path)

    def dump(self) -> str:
        return "\n\n".join(f"### {p}\n{c}" for p, c in sorted(self.live.items()))

    # -- transport ----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        command = request.url.path.removeprefix("/internal/memory/")
        if request.method == "GET" and command == "view":
            return self._view(request.url.params.get("path", ""))
        body: dict[str, Any] = json.loads(request.content or b"{}")
        handler = getattr(self, f"_{command}", None)
        if request.method != "POST" or handler is None:
            return _error(404, f"Error: unknown command {command}")
        return handler(body)

    def _under(self, prefix: str) -> list[str]:
        return sorted(p for p in self.live if p == prefix or p.startswith(f"{prefix}/"))

    def _view(self, raw: str) -> httpx.Response:
        path = raw.strip("/")
        if path in self.live:
            return _ok({"path": path, "is_directory": False, "content": self.live[path]})
        paths = sorted(self.live) if not path else self._under(path)
        if path and not paths:
            return _error(404, f"Error: {raw} not found")
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in paths:
            rel = entry[len(path) + 1 :] if path else entry
            segments = rel.split("/")
            if len(segments) <= 2:
                entries.append({"path": entry, "size": len(self.live[entry].encode())})
                continue
            marker = "/".join(([path] if path else []) + segments[:2]) + "/"
            if marker not in seen:
                seen.add(marker)
                entries.append({"path": marker, "size": None})
        return _ok({"path": path, "is_directory": True, "entries": entries})

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        path = body["path"].strip("/")
        if self._under(path):
            return _error(400, f"Error: File {path} already exists")
        self.live[path] = body["content"]
        return _ok(self._entry(path), status=201)

    def _str_replace(self, body: dict[str, Any]) -> httpx.Response:
        path = body["path"].strip("/")
        if path not in self.live:
            return _error(404, f"Error: File {path} not found")
        content, old = self.live[path], body["old_str"]
        count = content.count(old) if old else 0
        if count == 0:
            return _error(400, f"Error: old_str not found in {path}")
        if count > 1:
            return _error(400, f"Error: old_str appears multiple times in {path}")
        self.live[path] = content.replace(old, body["new_str"], 1)
        return _ok(self._entry(path))

    def _insert(self, body: dict[str, Any]) -> httpx.Response:
        path = body["path"].strip("/")
        if path not in self.live:
            return _error(404, f"Error: File {path} not found")
        lines = self.live[path].split("\n")
        if body["insert_line"] > len(lines):
            return _error(400, "Error: insert_line out of range")
        lines.insert(body["insert_line"], body["text"])
        self.live[path] = "\n".join(lines)
        return _ok(self._entry(path))

    def _delete(self, body: dict[str, Any]) -> httpx.Response:
        path = body["path"].strip("/")
        paths = self._under(path)
        if not path or not paths:
            return _error(404, f"Error: {path} not found")
        for p in paths:
            self.deleted[p] = self.live.pop(p)
        return _ok({"path": path, "deleted_count": len(paths)})

    def _rename(self, body: dict[str, Any]) -> httpx.Response:
        old, new = body["old_path"].strip("/"), body["new_path"].strip("/")
        paths = self._under(old)
        if not paths:
            return _error(404, f"Error: {old} not found")
        if self._under(new):
            return _error(400, f"Error: {new} already exists")
        for p in paths:
            self.live[new + p[len(old) :]] = self.live.pop(p)
        return _ok({"old_path": old, "new_path": new, "renamed_count": len(paths)})

    def _entry(self, path: str) -> dict[str, Any]:
        content = self.live[path]
        return {
            "path": path,
            "name": path.rsplit("/", 1)[-1],
            "content": content,
            "size": len(content.encode()),
            "created_at": "2026-09-26T00:00:00Z",
            "updated_at": "2026-09-26T00:00:00Z",
        }


def _ok(body: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body)


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail})
