"""Generic git activities shared across workflows."""

from src.activities.git.ops import (
    create_worktree,
    default_branch,
    ensure_repo,
    fix_missing_reason,
    push_branch,
    verify_fix_pushed,
)
from src.activities.git.pr import attach_label, close_pr, open_pr, pr_id, pr_number
from src.activities.git.schemas import PRRef, WorktreePath
from src.activities.git.targets import resolve_target_repos

__all__ = [
    "PRRef",
    "WorktreePath",
    "attach_label",
    "close_pr",
    "create_worktree",
    "default_branch",
    "ensure_repo",
    "fix_missing_reason",
    "open_pr",
    "pr_id",
    "pr_number",
    "push_branch",
    "resolve_target_repos",
    "verify_fix_pushed",
]
