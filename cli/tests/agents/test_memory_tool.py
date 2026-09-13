"""Tests for the memory tool's transport, presentation, and command handlers.

`src.agents._memory_backend` is exercised directly (formatting, HTTP error
mapping) and `src.agents.memory_tool`'s `@tool`-decorated handlers are
exercised end-to-end against a mocked HTTP transport (`httpx.MockTransport`)
standing in for the backend from issue #180. No live backend is reachable
from this sandbox, and `respx` isn't a project dependency, so `httpx`'s own
`MockTransport` is used instead — it requires no extra dependency and lets
tests assert on the exact request made as well as the response handling.

NOTE ON DYNAMIC EXECUTION: in this sandbox, `claude_agent_sdk` (imported by
`src.agents.memory_tool`, and transitively by `src.agents.__init__` — which
every `src.agents.*` import triggers) fails to import: it pulls in `mcp`,
which pulls in `pydantic`, which hits a `TypeError: _eval_type() got an
unexpected keyword argument 'prefer_fwd_module'` under this sandbox's only
available Python 3.14 build (a pydantic-2.12.5-vs-cpython-3.14.0rc2
prerelease incompatibility predating this change — confirmed by the same
failure on `tests/agents/test_hooks.py`, untouched by this work). This is
therefore not something introduced here, and not fixable from `cli/`. These
tests are written to run normally under `uv run pytest` in an environment
where that import succeeds (e.g. real CI); see the accompanying report for
how the logic was verified in the meantime.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.agents import _memory_backend as backend
from src.agents import memory_tool as mt

# =============================================================================
# fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def memory_api_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backend.MEMORY_API_URL_ENV, "https://memory.test")


@pytest.fixture
def install_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire `_build_client` to an `httpx.MockTransport` driven by `handler`."""

    def _install(handler: Any) -> None:
        def _build_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(
                base_url="https://memory.test", transport=httpx.MockTransport(handler)
            )

        monkeypatch.setattr(backend, "_build_client", _build_client)

    return _install


def _json_response(status: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json=body)


# =============================================================================
# presentation: number_lines / human_size / format_directory_listing
# =============================================================================


def test_number_lines_uses_six_char_right_aligned_1_indexed_numbers() -> None:
    assert backend.number_lines(["hello"], 1) == "     1\thello"


def test_number_lines_honors_a_non_1_start() -> None:
    assert backend.number_lines(["a", "b"], 41) == "    41\ta\n    42\tb"


def test_human_size_under_1k_is_plain_byte_count() -> None:
    assert backend.human_size(0) == "0"
    assert backend.human_size(1023) == "1023"


def test_human_size_kilobytes() -> None:
    assert backend.human_size(5632) == "5.5K"


def test_human_size_megabytes() -> None:
    assert backend.human_size(1_258_291) == "1.2M"


def test_format_directory_listing_empty_is_not_an_error() -> None:
    assert backend.format_directory_listing("", []) == "Directory: /\n(empty)"


def test_format_directory_listing_renders_size_and_directory_marker_distinctly() -> None:
    entries: list[dict[str, Any]] = [
        {"path": "notes/todo.md", "size": 42},
        {"path": "notes/big.md", "size": 5632},
        {"path": "deep/nested/", "size": None},
    ]
    text = backend.format_directory_listing("notes", entries)
    lines = text.splitlines()
    assert lines[0] == "Directory: notes"
    assert lines[1] == ""
    assert "notes/todo.md\t42" in lines
    assert "notes/big.md\t5.5K" in lines
    assert "deep/nested/\t<DIR>" in lines
    assert "None" not in text


# =============================================================================
# presentation: view_range parsing + slicing
# =============================================================================


def test_parse_view_range_none_means_whole_file() -> None:
    assert backend.parse_view_range(None) is None


def test_parse_view_range_valid() -> None:
    assert backend.parse_view_range([2, 5]) == (2, 5)
    assert backend.parse_view_range([2, -1]) == (2, -1)


@pytest.mark.parametrize(
    "raw",
    [
        [1],
        [1, 2, 3],
        ["a", 2],
        [0, 5],
        [5, 2],
    ],
)
def test_parse_view_range_rejects_malformed_input(raw: object) -> None:
    with pytest.raises(backend.MemoryToolError):
        backend.parse_view_range(raw)


def test_format_file_view_full_file_numbers_every_line() -> None:
    text = backend.format_file_view("a\nb\nc", "f.md", None)
    assert text == "     1\ta\n     2\tb\n     3\tc"


def test_format_file_view_range_slices_and_keeps_original_line_numbers() -> None:
    content = "\n".join(f"line{i}" for i in range(1, 11))  # 10 lines
    text = backend.format_file_view(content, "f.md", (3, 5))
    assert text == "     3\tline3\n     4\tline4\n     5\tline5"


def test_format_file_view_range_end_minus_one_means_to_end_of_file() -> None:
    content = "\n".join(f"line{i}" for i in range(1, 6))  # 5 lines
    text = backend.format_file_view(content, "f.md", (3, -1))
    assert text == "     3\tline3\n     4\tline4\n     5\tline5"


def test_format_file_view_range_clamps_end_beyond_file_length() -> None:
    content = "a\nb\nc"
    text = backend.format_file_view(content, "f.md", (2, 999))
    assert text == "     2\tb\n     3\tc"


def test_format_file_view_range_start_beyond_file_raises() -> None:
    with pytest.raises(backend.MemoryToolError):
        backend.format_file_view("a\nb", "f.md", (10, -1))


def test_format_file_view_truncates_over_16000_chars_with_clear_notice() -> None:
    content = "x" * 20_000
    text = backend.format_file_view(content, "big.md", None)
    assert "[TRUNCATED" in text
    body = text.split("\n\n[TRUNCATED")[0]
    # "     1\t" prefix (7 chars) + 16000 x's
    assert body == "     1\t" + "x" * 16_000


def test_format_file_view_line_count_ceiling() -> None:
    content = "\n".join("x" for _ in range(1_000_000))  # 1,000,000 lines
    text = backend.format_file_view(content, "huge.md", None)
    assert text == "File huge.md exceeds maximum line limit of 999,999 lines."


def test_format_file_view_at_exactly_the_ceiling_is_fine() -> None:
    content = "\n".join("x" for _ in range(999_999))
    text = backend.format_file_view(content, "huge.md", (1, 1))
    assert text == "     1\tx"


# =============================================================================
# view command (handler)
# =============================================================================


async def test_view_file_success(install_backend: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/internal/memory/view"
        assert dict(request.url.params) == {"path": "notes/todo.md"}
        return _json_response(
            200,
            {
                "path": "notes/todo.md",
                "is_directory": False,
                "content": "buy milk\nbuy eggs",
                "entries": None,
            },
        )

    install_backend(handler)
    result = await mt._view.handler({"path": "notes/todo.md"})
    assert result.get("is_error") is not True
    text = result["content"][0]["text"]
    assert text == "     1\tbuy milk\n     2\tbuy eggs"


async def test_view_root_omits_path_query_param(install_backend: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {}
        return _json_response(200, {"path": "", "is_directory": True, "entries": []})

    install_backend(handler)
    result = await mt._view.handler({})
    assert result.get("is_error") is not True
    assert result["content"][0]["text"] == "Directory: /\n(empty)"


async def test_view_directory_listing(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "path": "notes",
                "is_directory": True,
                "entries": [
                    {"path": "notes/todo.md", "size": 42},
                    {"path": "notes/archive/", "size": None},
                ],
            },
        )

    install_backend(handler)
    result = await mt._view.handler({"path": "notes"})
    text = result["content"][0]["text"]
    assert "notes/todo.md\t42" in text
    assert "notes/archive/\t<DIR>" in text


async def test_view_with_view_range(install_backend: Any) -> None:
    content = "\n".join(f"line{i}" for i in range(1, 11))

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            200, {"path": "f.md", "is_directory": False, "content": content, "entries": None}
        )

    install_backend(handler)
    result = await mt._view.handler({"path": "f.md", "view_range": [8, -1]})
    text = result["content"][0]["text"]
    assert text == "     8\tline8\n     9\tline9\n    10\tline10"


async def test_view_not_found_surfaces_backend_detail_as_clean_error(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(404, {"detail": "Error: nope not found"})

    install_backend(handler)
    result = await mt._view.handler({"path": "nope"})
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "Error: nope not found"


async def test_view_missing_env_var_is_a_clean_error_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(backend.MEMORY_API_URL_ENV, raising=False)
    result = await mt._view.handler({})
    assert result["is_error"] is True
    assert backend.MEMORY_API_URL_ENV in result["content"][0]["text"]


async def test_view_network_error_is_a_clean_error_not_an_exception(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    install_backend(handler)
    result = await mt._view.handler({})
    assert result["is_error"] is True
    assert "could not reach memory backend" in result["content"][0]["text"]


async def test_view_invalid_view_range_is_a_clean_error(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            200, {"path": "f.md", "is_directory": False, "content": "a\nb", "entries": None}
        )

    install_backend(handler)
    result = await mt._view.handler({"path": "f.md", "view_range": [0, 1]})
    assert result["is_error"] is True


# =============================================================================
# create command
# =============================================================================


async def test_create_success(install_backend: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/internal/memory/create"
        body = json.loads(request.content)
        assert body == {
            "path": "notes/todo.md",
            "content": "buy milk",
            "name": None,
            "description": None,
            "metadata": None,
        }
        return _json_response(
            201,
            {
                "path": "notes/todo.md",
                "name": "todo.md",
                "description": None,
                "metadata": None,
                "content": "buy milk",
                "size": 8,
                "created_at": "2026-08-18T00:00:00Z",
                "updated_at": "2026-08-18T00:00:00Z",
            },
        )

    install_backend(handler)
    result = await mt._create.handler({"path": "notes/todo.md", "content": "buy milk"})
    assert result.get("is_error") is not True
    assert "Created notes/todo.md" in result["content"][0]["text"]


async def test_create_already_exists(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(400, {"detail": "Error: File notes/todo.md already exists"})

    install_backend(handler)
    result = await mt._create.handler({"path": "notes/todo.md", "content": "x"})
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "Error: File notes/todo.md already exists"


# =============================================================================
# str_replace command
# =============================================================================


async def test_str_replace_success(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "path": "notes/todo.md",
                "name": "todo.md",
                "description": None,
                "metadata": None,
                "content": "buy bread",
                "size": 9,
                "created_at": "2026-08-18T00:00:00Z",
                "updated_at": "2026-08-18T00:00:00Z",
            },
        )

    install_backend(handler)
    result = await mt._str_replace.handler(
        {"path": "notes/todo.md", "old_str": "milk", "new_str": "bread"}
    )
    assert result.get("is_error") is not True
    assert "Updated notes/todo.md" in result["content"][0]["text"]


async def test_str_replace_multiple_occurrences_surfaces_line_numbers(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            400,
            {
                "detail": (
                    "Error: old_str appears multiple times in f.md (lines 1, 3); "
                    "old_str must be unique"
                )
            },
        )

    install_backend(handler)
    result = await mt._str_replace.handler({"path": "f.md", "old_str": "x", "new_str": "y"})
    assert result["is_error"] is True
    assert "lines 1, 3" in result["content"][0]["text"]


async def test_str_replace_not_found(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(404, {"detail": "Error: File nope.md not found"})

    install_backend(handler)
    result = await mt._str_replace.handler({"path": "nope.md", "old_str": "a", "new_str": "b"})
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "Error: File nope.md not found"


# =============================================================================
# insert command
# =============================================================================


async def test_insert_success(install_backend: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"path": "f.md", "insert_line": 0, "text": "new first line"}
        return _json_response(
            200,
            {
                "path": "f.md",
                "name": "f.md",
                "description": None,
                "metadata": None,
                "content": "new first line\nold content",
                "size": 27,
                "created_at": "2026-08-18T00:00:00Z",
                "updated_at": "2026-08-18T00:00:00Z",
            },
        )

    install_backend(handler)
    result = await mt._insert.handler({"path": "f.md", "insert_line": 0, "text": "new first line"})
    assert result.get("is_error") is not True
    assert "Inserted into f.md after line 0" in result["content"][0]["text"]


async def test_insert_out_of_range(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(
            400, {"detail": "Error: insert_line 99 is out of range for f.md (file has 2 lines)"}
        )

    install_backend(handler)
    result = await mt._insert.handler({"path": "f.md", "insert_line": 99, "text": "x"})
    assert result["is_error"] is True
    assert "out of range" in result["content"][0]["text"]


# =============================================================================
# delete command
# =============================================================================


async def test_delete_success(install_backend: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"path": "notes"}
        return _json_response(200, {"path": "notes", "deleted_count": 3})

    install_backend(handler)
    result = await mt._delete.handler({"path": "notes"})
    assert result.get("is_error") is not True
    assert result["content"][0]["text"] == "Deleted 3 entries under notes"


async def test_delete_singular_entry_grammar(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"path": "notes/a.md", "deleted_count": 1})

    install_backend(handler)
    result = await mt._delete.handler({"path": "notes/a.md"})
    assert result["content"][0]["text"] == "Deleted 1 entry under notes/a.md"


async def test_delete_root_rejected(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(400, {"detail": "Error: cannot delete the memory root"})

    install_backend(handler)
    result = await mt._delete.handler({"path": ""})
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "Error: cannot delete the memory root"


async def test_delete_not_found(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(404, {"detail": "Error: nope not found"})

    install_backend(handler)
    result = await mt._delete.handler({"path": "nope"})
    assert result["is_error"] is True


# =============================================================================
# rename command
# =============================================================================


async def test_rename_success(install_backend: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"old_path": "a.md", "new_path": "b.md"}
        return _json_response(200, {"old_path": "a.md", "new_path": "b.md", "renamed_count": 1})

    install_backend(handler)
    result = await mt._rename.handler({"old_path": "a.md", "new_path": "b.md"})
    assert result.get("is_error") is not True
    assert result["content"][0]["text"] == "Renamed a.md to b.md (1 entries)"


async def test_rename_destination_exists(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(400, {"detail": "Error: b.md already exists"})

    install_backend(handler)
    result = await mt._rename.handler({"old_path": "a.md", "new_path": "b.md"})
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "Error: b.md already exists"


async def test_rename_source_not_found(install_backend: Any) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(404, {"detail": "Error: a.md not found"})

    install_backend(handler)
    result = await mt._rename.handler({"old_path": "a.md", "new_path": "b.md"})
    assert result["is_error"] is True


# =============================================================================
# server assembly
# =============================================================================


def test_memory_tool_names_are_fully_qualified_for_allowed_tools() -> None:
    assert mt.MEMORY_TOOL_NAMES == [
        "mcp__memory__view",
        "mcp__memory__create",
        "mcp__memory__str_replace",
        "mcp__memory__insert",
        "mcp__memory__delete",
        "mcp__memory__rename",
    ]


def test_memory_mcp_server_builds_a_named_sdk_server() -> None:
    server = mt.memory_mcp_server()
    assert server["type"] == "sdk"
    assert server["name"] == mt.MEMORY_SERVER_NAME
