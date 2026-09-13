"""Internal memory endpoints — container-initiated agent memory storage.

Implements Anthropic's ``memory_20250818`` tool contract (view, create,
str_replace, insert, delete, rename) as dumb, workspace-scoped storage — no
line numbering, formatting, or truncation; that's the CLI-side tool's job.
Every DB call below is scoped by ``principal.workspace_id`` — never by a
client-supplied path param or request field.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.database import get_session
from api.database.memory import (
    db_count_memory_entries,
    db_create_memory_entry,
    db_delete_memory_entries,
    db_get_memory_entries_by_paths,
    db_get_memory_entry,
    db_list_memory_entries,
    db_rename_memory_entries,
    db_update_memory_entry_content,
)
from api.models.memory import MemoryEntry
from api.routers.internal.dependencies import MemoryPrincipal, get_memory_principal

from .paths import is_root, normalize_memory_path
from .schemas import (
    MemoryCreateRequest,
    MemoryDeleteRequest,
    MemoryDeleteResponse,
    MemoryEntryResponse,
    MemoryEntrySummary,
    MemoryInsertRequest,
    MemoryRenameRequest,
    MemoryRenameResponse,
    MemoryStrReplaceRequest,
    MemoryViewResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/memory", tags=["Internal Memory"])

MAX_CONTENT_BYTES = 100_000
MAX_ENTRIES_PER_WORKSPACE = 2000
DIRECTORY_LISTING_DEPTH = 2


def _byte_size(content: str) -> int:
    return len(content.encode("utf-8"))


def _check_size(content: str) -> None:
    size = _byte_size(content)
    if size > MAX_CONTENT_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Error: content exceeds maximum size of {MAX_CONTENT_BYTES} bytes",
        )


def _to_entry_response(entry: MemoryEntry) -> MemoryEntryResponse:
    return MemoryEntryResponse(
        path=entry.path,
        name=entry.name,
        description=entry.description,
        metadata=entry.entry_metadata,
        content=entry.content,
        size=_byte_size(entry.content),
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def _relative_path(path: str, prefix: str) -> str:
    if not prefix:
        return path
    return path[len(prefix) + 1 :]


def _directory_marker(prefix: str, segments: list[str]) -> str:
    joined = "/".join(segments)
    full = f"{prefix}/{joined}" if prefix else joined
    return full + "/"


def _build_directory_listing(entries: list[MemoryEntry], prefix: str) -> list[MemoryEntrySummary]:
    """Collapse entries under prefix into a listing at most 2 levels deep,
    folding deeper entries into a single directory-marker row (size=None)."""
    seen_markers: set[str] = set()
    result: list[MemoryEntrySummary] = []
    for entry in entries:
        rel = _relative_path(entry.path, prefix)
        segments = rel.split("/")
        if len(segments) <= DIRECTORY_LISTING_DEPTH:
            result.append(MemoryEntrySummary(path=entry.path, size=_byte_size(entry.content)))
        else:
            marker = _directory_marker(prefix, segments[:DIRECTORY_LISTING_DEPTH])
            if marker not in seen_markers:
                seen_markers.add(marker)
                result.append(MemoryEntrySummary(path=marker, size=None))
    return result


def _find_occurrences(content: str, needle: str) -> list[int]:
    indices: list[int] = []
    start = 0
    while True:
        idx = content.find(needle, start)
        if idx == -1:
            break
        indices.append(idx)
        start = idx + len(needle)
    return indices


def _line_number(content: str, index: int) -> int:
    return content.count("\n", 0, index) + 1


def _ancestor_paths(path: str) -> list[str]:
    segments = path.split("/")
    return ["/".join(segments[:i]) for i in range(1, len(segments))]


@router.get("/view", operation_id="view_memory", response_model=MemoryViewResponse)
def view_memory(
    path: str | None = Query(default=None, description="Path to view; omit for the root"),
    principal: MemoryPrincipal = Depends(get_memory_principal),
    db: Session = Depends(get_session),
) -> MemoryViewResponse:
    """View a file's raw content, or list a directory (2 levels deep).

    An empty workspace's root view returns an empty listing, not an error.
    """
    normalized = normalize_memory_path(path, allow_root=True)

    if not is_root(normalized):
        exact = db_get_memory_entry(db, principal.workspace_id, normalized)
        if exact is not None:
            return MemoryViewResponse(path=normalized, is_directory=False, content=exact.content)

    entries = db_list_memory_entries(db, principal.workspace_id, normalized)
    if not is_root(normalized) and not entries:
        raise HTTPException(status_code=404, detail=f"Error: {path} not found")

    listing = _build_directory_listing(entries, normalized)
    return MemoryViewResponse(path=normalized, is_directory=True, entries=listing)


@router.post(
    "/create",
    operation_id="create_memory_entry",
    response_model=MemoryEntryResponse,
    status_code=201,
)
def create_memory_entry(
    request: MemoryCreateRequest,
    principal: MemoryPrincipal = Depends(get_memory_principal),
    db: Session = Depends(get_session),
) -> MemoryEntryResponse:
    """Create a new memory entry.

    Rejects a path that would leave a file and a "directory" (another
    entry's path prefix) sharing the same name — otherwise a later view on
    the file would silently hide entries stored under it.
    """
    normalized = normalize_memory_path(request.path, allow_root=False)
    _check_size(request.content)

    count = db_count_memory_entries(db, principal.workspace_id)
    if count >= MAX_ENTRIES_PER_WORKSPACE:
        raise HTTPException(
            status_code=400,
            detail=f"Error: workspace memory store is full (maximum {MAX_ENTRIES_PER_WORKSPACE} entries)",
        )

    if db_list_memory_entries(db, principal.workspace_id, normalized):
        raise HTTPException(status_code=400, detail=f"Error: File {normalized} already exists")

    ancestors = _ancestor_paths(normalized)
    if ancestors and db_get_memory_entries_by_paths(db, principal.workspace_id, ancestors):
        raise HTTPException(
            status_code=400,
            detail=f"Error: cannot create {normalized} — an ancestor path already exists as a file",
        )

    name = request.name or normalized.rsplit("/", 1)[-1]

    try:
        entry = db_create_memory_entry(
            db,
            workspace_id=principal.workspace_id,
            path=normalized,
            content=request.content,
            name=name,
            description=request.description,
            entry_metadata=request.metadata,
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400, detail=f"Error: File {normalized} already exists"
        ) from None

    return _to_entry_response(entry)


@router.post(
    "/str_replace", operation_id="str_replace_memory_entry", response_model=MemoryEntryResponse
)
def str_replace_memory_entry(
    request: MemoryStrReplaceRequest,
    principal: MemoryPrincipal = Depends(get_memory_principal),
    db: Session = Depends(get_session),
) -> MemoryEntryResponse:
    """Replace a unique substring in a file's content."""
    normalized = normalize_memory_path(request.path, allow_root=False)
    entry = db_get_memory_entry(db, principal.workspace_id, normalized, for_update=True)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Error: File {normalized} not found")

    if not request.old_str:
        raise HTTPException(status_code=400, detail="Error: old_str must not be empty")

    occurrences = _find_occurrences(entry.content, request.old_str)
    if not occurrences:
        raise HTTPException(status_code=400, detail=f"Error: old_str not found in {normalized}")
    if len(occurrences) > 1:
        line_numbers = ", ".join(str(_line_number(entry.content, idx)) for idx in occurrences)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Error: old_str appears multiple times in {normalized} "
                f"(lines {line_numbers}); old_str must be unique"
            ),
        )

    idx = occurrences[0]
    new_content = (
        entry.content[:idx] + request.new_str + entry.content[idx + len(request.old_str) :]
    )
    _check_size(new_content)

    updated = db_update_memory_entry_content(db, principal.workspace_id, normalized, new_content)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Error: File {normalized} not found")
    return _to_entry_response(updated)


@router.post("/insert", operation_id="insert_memory_entry", response_model=MemoryEntryResponse)
def insert_memory_entry(
    request: MemoryInsertRequest,
    principal: MemoryPrincipal = Depends(get_memory_principal),
    db: Session = Depends(get_session),
) -> MemoryEntryResponse:
    """Insert a line of text after insert_line (0 = beginning of file)."""
    normalized = normalize_memory_path(request.path, allow_root=False)
    entry = db_get_memory_entry(db, principal.workspace_id, normalized, for_update=True)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Error: File {normalized} not found")

    lines = entry.content.split("\n")
    if request.insert_line > len(lines):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Error: insert_line {request.insert_line} is out of range for {normalized} "
                f"(file has {len(lines)} lines)"
            ),
        )

    lines.insert(request.insert_line, request.text)
    new_content = "\n".join(lines)
    _check_size(new_content)

    updated = db_update_memory_entry_content(db, principal.workspace_id, normalized, new_content)
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Error: File {normalized} not found")
    return _to_entry_response(updated)


@router.post("/delete", operation_id="delete_memory_entry", response_model=MemoryDeleteResponse)
def delete_memory_entry(
    request: MemoryDeleteRequest,
    principal: MemoryPrincipal = Depends(get_memory_principal),
    db: Session = Depends(get_session),
) -> MemoryDeleteResponse:
    """Delete a file, or everything under a path prefix. Rejects the root."""
    normalized = normalize_memory_path(request.path, allow_root=True)
    if is_root(normalized):
        raise HTTPException(status_code=400, detail="Error: cannot delete the memory root")

    deleted = db_delete_memory_entries(db, principal.workspace_id, normalized)
    if deleted == 0:
        raise HTTPException(status_code=404, detail=f"Error: {normalized} not found")

    return MemoryDeleteResponse(path=normalized, deleted_count=deleted)


@router.post("/rename", operation_id="rename_memory_entry", response_model=MemoryRenameResponse)
def rename_memory_entry(
    request: MemoryRenameRequest,
    principal: MemoryPrincipal = Depends(get_memory_principal),
    db: Session = Depends(get_session),
) -> MemoryRenameResponse:
    """Rename/move a file or directory. Errors if the destination exists."""
    old_normalized = normalize_memory_path(request.old_path, allow_root=False)
    new_normalized = normalize_memory_path(request.new_path, allow_root=False)

    if old_normalized == new_normalized:
        raise HTTPException(status_code=400, detail="Error: old_path and new_path are the same")

    if db_list_memory_entries(db, principal.workspace_id, new_normalized):
        raise HTTPException(status_code=400, detail=f"Error: {new_normalized} already exists")

    ancestors = _ancestor_paths(new_normalized)
    if ancestors and db_get_memory_entries_by_paths(db, principal.workspace_id, ancestors):
        raise HTTPException(
            status_code=400,
            detail=f"Error: cannot rename to {new_normalized} — an ancestor path already exists as a file",
        )

    try:
        renamed = db_rename_memory_entries(
            db, principal.workspace_id, old_normalized, new_normalized
        )
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400, detail=f"Error: {new_normalized} already exists"
        ) from None

    if renamed == 0:
        raise HTTPException(status_code=404, detail=f"Error: {old_normalized} not found")

    return MemoryRenameResponse(
        old_path=old_normalized, new_path=new_normalized, renamed_count=renamed
    )
