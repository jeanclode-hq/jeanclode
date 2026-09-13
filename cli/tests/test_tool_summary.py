from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

from src.agents.utils import tool_summary


def _block(name: str, **inputs: object) -> MagicMock:
    b = MagicMock()
    b.name = name
    b.input = inputs
    return b


def test_strips_workspace_prefix_from_read(tmp_path: Path) -> None:
    abs_path = str(tmp_path / "src" / "agents" / "schemas.py")
    summary = tool_summary(_block("Read", file_path=abs_path), cwd=tmp_path)
    assert summary == "src/agents/schemas.py"


def test_strips_realpath_prefix_for_macos_var_folders(tmp_path: Path) -> None:
    # macOS resolves /var/folders/... to /private/var/folders/... — strip both.
    realpath = os.path.realpath(tmp_path)
    abs_path = str(Path(realpath) / "x.py")
    summary = tool_summary(_block("Read", file_path=abs_path), cwd=tmp_path)
    assert summary == "x.py"


def test_strips_grep_path(tmp_path: Path) -> None:
    abs_path = str(tmp_path / "cli")
    summary = tool_summary(_block("Grep", pattern="foo", path=abs_path), cwd=tmp_path)
    assert summary == "foo in cli"


def test_leaves_unmatched_path_alone(tmp_path: Path) -> None:
    summary = tool_summary(_block("Read", file_path="/etc/hosts"), cwd=tmp_path)
    assert summary == "/etc/hosts"


def test_no_cwd_returns_raw_path() -> None:
    summary = tool_summary(_block("Read", file_path="/tmp/x.py"))
    assert summary == "/tmp/x.py"
