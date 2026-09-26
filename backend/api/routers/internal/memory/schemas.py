"""Request and response schemas for the internal memory endpoints."""

from datetime import datetime

from pydantic import BaseModel, Field


class MemoryEntrySummary(BaseModel):
    """One row in a directory listing."""

    path: str = Field(description="Path relative to the workspace memory root")
    size: int | None = Field(
        default=None,
        description=(
            "Byte length of content for a file entry. Null for a directory "
            "entry (a path prefix with no entry stored at it directly)."
        ),
    )


class MemoryViewResponse(BaseModel):
    """Response for the view command.

    Exactly one of ``content`` / ``entries`` is set, selected by
    ``is_directory``.
    """

    path: str = Field(description="Normalized path that was viewed (empty string = root)")
    is_directory: bool = Field(description="True if this is a directory listing, not a file")
    content: str | None = Field(default=None, description="Raw file content, when not a directory")
    entries: list[MemoryEntrySummary] | None = Field(
        default=None, description="Directory listing (2 levels deep), when is_directory is True"
    )


class MemoryEntryResponse(BaseModel):
    """Response for create / str_replace / insert — the entry's state after the write."""

    path: str = Field(description="Normalized path of the entry")
    name: str = Field(description="Entry name")
    description: str | None = Field(default=None, description="Entry description")
    metadata: dict | None = Field(default=None, description="Arbitrary caller-supplied metadata")
    content: str = Field(description="Full content after the write")
    size: int = Field(description="Byte length of content")
    created_at: datetime = Field(description="Creation timestamp")
    updated_at: datetime = Field(description="Last update timestamp")


class MemoryCreateRequest(BaseModel):
    """Request body for the create command."""

    path: str = Field(description="Path to create")
    content: str = Field(description="Initial file content")
    name: str | None = Field(default=None, description="Entry name; defaults to path's basename")
    description: str | None = Field(default=None, description="Optional entry description")
    metadata: dict | None = Field(default=None, description="Optional arbitrary metadata")


class MemoryStrReplaceRequest(BaseModel):
    """Request body for the str_replace command."""

    path: str = Field(description="Path of the file to edit")
    old_str: str = Field(description="Exact substring to find; must occur exactly once")
    new_str: str = Field(description="Replacement text")


class MemoryInsertRequest(BaseModel):
    """Request body for the insert command."""

    path: str = Field(description="Path of the file to edit")
    insert_line: int = Field(
        ge=0,
        description="Line number to insert after (0-indexed at the start of the line count; 0 = beginning of file)",
    )
    text: str = Field(description="Text to insert as a new line")


class MemoryDeleteRequest(BaseModel):
    """Request body for the delete command."""

    path: str = Field(description="Path to delete; deletes everything under it if it's a directory")


class MemoryDeleteResponse(BaseModel):
    """Response for the delete command."""

    path: str = Field(description="Normalized path that was deleted")
    deleted_count: int = Field(description="Number of entries removed")


class MemoryRenameRequest(BaseModel):
    """Request body for the rename command."""

    old_path: str = Field(description="Existing path (file or directory prefix)")
    new_path: str = Field(description="Destination path; must not already exist")


class MemoryRenameResponse(BaseModel):
    """Response for the rename command."""

    old_path: str = Field(description="Normalized source path")
    new_path: str = Field(description="Normalized destination path")
    renamed_count: int = Field(description="Number of entries moved")


class MemoryCurationDueResponse(BaseModel):
    """Entries the curator hasn't reviewed since they last changed."""

    paths: list[str] = Field(description="Paths due for curation, in path order")


class MemoryCurationMarkRequest(BaseModel):
    """Request body for marking entries as curated."""

    paths: list[str] = Field(description="Paths the curator has just reviewed")


class MemoryCurationMarkResponse(BaseModel):
    """Response for marking entries as curated."""

    marked_count: int = Field(description="Number of live entries stamped")
