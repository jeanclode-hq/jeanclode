"""Generic git/PR schemas shared across workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class WorktreePath(BaseModel):
    path: Path
    branch: str
    # See create_worktree. Defaults to "" for callers/tests that build a
    # WorktreePath directly instead of through it.
    placeholder_sha: str = ""


class PRRef(BaseModel):
    url: str
    branch: str
    platform: Literal["github", "gitlab"]
