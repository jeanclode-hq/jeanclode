"""In-process SDK MCP tool giving agents workspace-scoped persistent memory.

Implements Anthropic's ``memory_20250818`` six-command contract (view /
create / str_replace / insert / delete / rename) as a custom SDK tool.
Transport and presentation logic live in ``src.agents._memory_backend``.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool, create_sdk_mcp_server, tool

from src.agents._memory_backend import (
    MemoryToolError,
    backend_request,
    error_detail,
    error_result,
    format_directory_listing,
    format_file_view,
    human_size,
    parse_view_range,
    text_result,
)

MEMORY_SERVER_NAME = "memory"

_COMMAND_NAMES = ("view", "create", "str_replace", "insert", "delete", "rename")
MEMORY_TOOL_NAMES: list[str] = [f"mcp__{MEMORY_SERVER_NAME}__{name}" for name in _COMMAND_NAMES]

_VIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to view. Omit (or leave empty) for the workspace root.",
        },
        "view_range": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
            "description": (
                "Optional [start_line, end_line], 1-indexed and inclusive. "
                "end_line == -1 means 'to end of file'. Ignored for directories."
            ),
        },
    },
    "required": [],
}

_CREATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path to create."},
        "content": {"type": "string", "description": "Initial file content."},
        "name": {
            "type": ["string", "null"],
            "description": "Entry name; defaults to the path's basename.",
        },
        "description": {"type": ["string", "null"], "description": "Optional description."},
        "metadata": {"type": ["object", "null"], "description": "Optional arbitrary metadata."},
    },
    "required": ["path", "content"],
}

_STR_REPLACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path of the file to edit."},
        "old_str": {
            "type": "string",
            "description": "Exact substring to find; must occur exactly once.",
        },
        "new_str": {"type": "string", "description": "Replacement text."},
    },
    "required": ["path", "old_str", "new_str"],
}

_INSERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path of the file to edit."},
        "insert_line": {
            "type": "integer",
            "minimum": 0,
            "description": "Line number to insert after. 0 = beginning of file.",
        },
        "text": {"type": "string", "description": "Text to insert as a new line."},
    },
    "required": ["path", "insert_line", "text"],
}

_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to delete. Deletes everything under it if it's a directory.",
        },
    },
    "required": ["path"],
}

_RENAME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "old_path": {"type": "string", "description": "Existing path (file or directory)."},
        "new_path": {"type": "string", "description": "Destination path; must not already exist."},
    },
    "required": ["old_path", "new_path"],
}


@tool(
    "view",
    "View a memory file's content, or list a directory. Omit path for the workspace root.",
    _VIEW_SCHEMA,
)
async def _view(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path") or "")
    try:
        view_range = parse_view_range(args.get("view_range"))
        params = {"path": path} if path else None
        status, data = await backend_request("GET", "/internal/memory/view", params=params)
        if status >= 400:
            return error_result(error_detail(data))

        if data.get("is_directory"):
            return text_result(
                format_directory_listing(data.get("path", path), data.get("entries") or [])
            )
        text = format_file_view(data.get("content") or "", data.get("path", path), view_range)
        return text_result(text)
    except MemoryToolError as exc:
        return error_result(str(exc))


@tool("create", "Create a new memory file at `path` with the given `content`.", _CREATE_SCHEMA)
async def _create(args: dict[str, Any]) -> dict[str, Any]:
    try:
        status, data = await backend_request(
            "POST",
            "/internal/memory/create",
            json_body={
                "path": args["path"],
                "content": args["content"],
                "name": args.get("name"),
                "description": args.get("description"),
                "metadata": args.get("metadata"),
            },
        )
        if status >= 400:
            return error_result(error_detail(data))
        return text_result(
            f"Created {data.get('path', args['path'])} ({human_size(data.get('size', 0))})"
        )
    except MemoryToolError as exc:
        return error_result(str(exc))


@tool(
    "str_replace",
    "Replace the unique occurrence of `old_str` with `new_str` in a memory file.",
    _STR_REPLACE_SCHEMA,
)
async def _str_replace(args: dict[str, Any]) -> dict[str, Any]:
    try:
        status, data = await backend_request(
            "POST",
            "/internal/memory/str_replace",
            json_body={
                "path": args["path"],
                "old_str": args["old_str"],
                "new_str": args["new_str"],
            },
        )
        if status >= 400:
            return error_result(error_detail(data))
        return text_result(f"Updated {data.get('path', args['path'])}")
    except MemoryToolError as exc:
        return error_result(str(exc))


@tool(
    "insert",
    "Insert `text` as a new line after `insert_line` (0 = beginning of file).",
    _INSERT_SCHEMA,
)
async def _insert(args: dict[str, Any]) -> dict[str, Any]:
    try:
        status, data = await backend_request(
            "POST",
            "/internal/memory/insert",
            json_body={
                "path": args["path"],
                "insert_line": args["insert_line"],
                "text": args["text"],
            },
        )
        if status >= 400:
            return error_result(error_detail(data))
        return text_result(
            f"Inserted into {data.get('path', args['path'])} after line {args['insert_line']}"
        )
    except MemoryToolError as exc:
        return error_result(str(exc))


@tool("delete", "Delete a memory file, or everything under a directory path.", _DELETE_SCHEMA)
async def _delete(args: dict[str, Any]) -> dict[str, Any]:
    try:
        status, data = await backend_request(
            "POST", "/internal/memory/delete", json_body={"path": args["path"]}
        )
        if status >= 400:
            return error_result(error_detail(data))
        count = data.get("deleted_count", 0)
        noun = "entry" if count == 1 else "entries"
        return text_result(f"Deleted {count} {noun} under {args['path']}")
    except MemoryToolError as exc:
        return error_result(str(exc))


@tool("rename", "Rename/move a memory file or directory to `new_path`.", _RENAME_SCHEMA)
async def _rename(args: dict[str, Any]) -> dict[str, Any]:
    try:
        status, data = await backend_request(
            "POST",
            "/internal/memory/rename",
            json_body={"old_path": args["old_path"], "new_path": args["new_path"]},
        )
        if status >= 400:
            return error_result(error_detail(data))
        return text_result(
            f"Renamed {args['old_path']} to {args['new_path']} "
            f"({data.get('renamed_count', 0)} entries)"
        )
    except MemoryToolError as exc:
        return error_result(str(exc))


_TOOLS: list[SdkMcpTool[Any]] = [_view, _create, _str_replace, _insert, _delete, _rename]


def memory_mcp_server() -> McpSdkServerConfig:
    """Register under ClaudeAgentOptions.mcp_servers; authorize MEMORY_TOOL_NAMES in allowed_tools."""
    return create_sdk_mcp_server(name=MEMORY_SERVER_NAME, tools=_TOOLS)
