"""Database operations for MemoryEntry — plain CRUD, no path validation.

Every read and write here sees live entries only: ``delete`` stamps
``deleted_at`` and the row is never served again.
"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session

from api.models.memory import MemoryEntry
from api.models.workspaces import Workspace


def _live(workspace_id: UUID):
    return (MemoryEntry.workspace_id == workspace_id, MemoryEntry.deleted_at.is_(None))


def db_get_memory_entries_by_paths(
    db: Session,
    workspace_id: UUID,
    paths: list[str],
) -> list[MemoryEntry]:
    """Get every entry whose path exactly matches one of ``paths``."""
    if not paths:
        return []
    return db.query(MemoryEntry).filter(*_live(workspace_id), MemoryEntry.path.in_(paths)).all()


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
    query = db.query(MemoryEntry).filter(*_live(workspace_id), MemoryEntry.path == path)
    if for_update:
        query = query.with_for_update()
    return query.first()


def db_list_memory_entries(
    db: Session,
    workspace_id: UUID,
    prefix: str = "",
) -> list[MemoryEntry]:
    """List entries at or under a path prefix. Empty prefix lists everything."""
    query = db.query(MemoryEntry).filter(*_live(workspace_id))
    if prefix:
        query = query.filter(or_(MemoryEntry.path == prefix, MemoryEntry.path.like(f"{prefix}/%")))
    return query.order_by(MemoryEntry.path).all()


def db_count_memory_entries(db: Session, workspace_id: UUID) -> int:
    """Count all memory entries in a workspace."""
    return db.query(func.count(MemoryEntry.id)).filter(*_live(workspace_id)).scalar() or 0


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
    """Soft-delete the entry at path plus everything under it as a prefix."""
    deleted = (
        db.query(MemoryEntry)
        .filter(
            *_live(workspace_id),
            or_(MemoryEntry.path == path, MemoryEntry.path.like(f"{path}/%")),
        )
        .update({MemoryEntry.deleted_at: func.now()}, synchronize_session=False)
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


def _due_for_curation():
    return or_(MemoryEntry.curated_at.is_(None), MemoryEntry.updated_at > MemoryEntry.curated_at)


def db_list_memory_paths_due_for_curation(db: Session, workspace_id: UUID) -> list[str]:
    """Paths of live entries the curator hasn't seen since they last changed."""
    rows = (
        db.query(MemoryEntry.path)
        .filter(*_live(workspace_id), _due_for_curation())
        .order_by(MemoryEntry.path)
        .all()
    )
    return [row.path for row in rows]


def db_mark_memory_entries_curated(db: Session, workspace_id: UUID, paths: list[str]) -> int:
    """Stamp ``curated_at`` on the live entries at ``paths``, leaving ``updated_at`` alone."""
    if not paths:
        return 0
    marked = (
        db.query(MemoryEntry)
        .filter(*_live(workspace_id), MemoryEntry.path.in_(paths))
        .update(
            {MemoryEntry.curated_at: func.now(), MemoryEntry.updated_at: MemoryEntry.updated_at},
            synchronize_session=False,
        )
    )
    db.commit()
    return marked


def db_claim_workspaces_due_for_memory_curation(
    db: Session, *, cadence: timedelta, limit: int
) -> list[UUID]:
    """Claim workspaces whose last curation is older than ``cadence`` and that
    have at least one entry due.

    ``FOR UPDATE SKIP LOCKED`` plus stamping ``memory_curated_at`` in the same
    transaction keeps two backend pods from dispatching the same workspace.
    """
    has_due_entry = exists().where(
        MemoryEntry.workspace_id == Workspace.id,
        MemoryEntry.deleted_at.is_(None),
        _due_for_curation(),
    )
    workspaces = (
        db.query(Workspace)
        .filter(
            or_(
                Workspace.memory_curated_at.is_(None),
                Workspace.memory_curated_at <= func.now() - cadence,
            ),
            has_due_entry,
        )
        .order_by(Workspace.memory_curated_at.asc().nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True, of=Workspace)
        .all()
    )
    for workspace in workspaces:
        workspace.memory_curated_at = func.now()
    ids = [workspace.id for workspace in workspaces]
    db.commit()
    return ids
