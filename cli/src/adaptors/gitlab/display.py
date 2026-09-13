"""GitLab display — same shape as GitHub display (script labels are platform-agnostic)."""

from __future__ import annotations

from src.adaptors.github.display import GithubDisplay


class GitlabDisplay(GithubDisplay):
    """Reuse GithubDisplay — same scripts, same labels."""
