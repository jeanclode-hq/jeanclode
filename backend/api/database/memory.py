"""Database operations for MemoryEntry — plain CRUD, no path validation."""

from uuid import UUID

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from api.models.memory import MemoryEntry


def db_get_memory_entries_by_paths(
    db: Session,
    workspace_id: UUID,
    paths: list[str],
) -> list[MemoryEntry]:
    """Get every entry whose path exactly matches one of ``paths``."""
    if not paths:
        return []
    return (
        db.query(MemoryEntry)
        .filter(MemoryEntry.workspace_id == workspace_id, MemoryEntry.path.in_(paths))
        .all()
    )


def db_get_memory_entry(
    db: Session,
    workspace_id: UUID,
    path: str,
    *,
    for_update: bool = False,
) -> MemoryEntry | None:
    """Get a single memory entry by its exact path.

    ``for_update=True`` locks the row for the rest of the transaction — used
    by read-modify-write callers (str_replace, insert) so a concurrent
    writer blocks instead of silently overwriting the first edit.
    """
    query = db.query(MemoryEntry).filter(
        MemoryEntry.workspace_id == workspace_id, MemoryEntry.path == path
    )
    if for_update:
        query = query.with_for_update()
    return query.first()


def db_list_memory_entries(
    db: Session,
    workspace_id: UUID,
    prefix: str = "",
) -> list[MemoryEntry]:
    """List entries at or under a path prefix. Empty prefix lists everything."""
    query = db.query(MemoryEntry).filter(MemoryEntry.workspace_id == workspace_id)
    if prefix:
        query = query.filter(or_(MemoryEntry.path == prefix, MemoryEntry.path.like(f"{prefix}/%")))
    return query.order_by(MemoryEntry.path).all()


def db_count_memory_entries(db: Session, workspace_id: UUID) -> int:
    """Count all memory entries in a workspace."""
    return (
        db.query(func.count(MemoryEntry.id))
        .filter(MemoryEntry.workspace_id == workspace_id)
        .scalar()
        or 0
    )


def db_create_memory_entry(
    db: Session,
    workspace_id: UUID,
    path: str,
    content: str,
    name: str,
    description: str | None = None,
    entry_metadata: dict | None = None,
) -> MemoryEntry:
    """Insert a new memory entry. Raises IntegrityError on a duplicate path."""
    entry = MemoryEntry(
        workspace_id=workspace_id,
        path=path,
        content=content,
        name=name,
        description=description,
        entry_metadata=entry_metadata,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def db_update_memory_entry_content(
    db: Session,
    workspace_id: UUID,
    path: str,
    content: str,
) -> MemoryEntry | None:
    """Overwrite an entry's content. Returns None if no entry exists at path."""
    entry = db_get_memory_entry(db, workspace_id, path)
    if entry is None:
        return None
    entry.content = content
    db.commit()
    db.refresh(entry)
    return entry


def db_delete_memory_entries(db: Session, workspace_id: UUID, path: str) -> int:
    """Delete the entry at path plus everything under it as a prefix."""
    deleted = (
        db.query(MemoryEntry)
        .filter(
            MemoryEntry.workspace_id == workspace_id,
            or_(MemoryEntry.path == path, MemoryEntry.path.like(f"{path}/%")),
        )
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted


def db_rename_memory_entries(
    db: Session,
    workspace_id: UUID,
    old_path: str,
    new_path: str,
) -> int:
    """Rename an entry, or every entry under old_path as a prefix.

    Raises IntegrityError if any computed destination path already exists.
    """
    entries = db_list_memory_entries(db, workspace_id, old_path)
    if not entries:
        return 0

    prefix_len = len(old_path)
    for entry in entries:
        entry.path = new_path + entry.path[prefix_len:]
    db.commit()
    return len(entries)
