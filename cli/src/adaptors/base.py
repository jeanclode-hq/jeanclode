"""Base adaptor protocol — defines the interface every source adaptor implements.

Each adaptor handles a specific platform (Sentry, GitHub, GitLab, ...) and
exposes one or more workflows via its `skills` map (command -> skill name).
URL match selects the adaptor; CLI subcommand selects the skill within it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from src.adaptors.display import Display


class Adaptor(Protocol):
    """Interface for platform adaptors."""

    @property
    def name(self) -> str:
        """Human-readable name (e.g. "sentry", "github", "gitlab")."""
        ...

    @property
    def default_command(self) -> str:
        """Command used when none is given on the CLI (must be a key of `skills`)."""
        ...

    @property
    def skills(self) -> dict[str, str]:
        """Map of CLI subcommand -> plugin skill name (e.g. {"review": "code-review"})."""
        ...

    def select_command(self, url: str) -> str:
        """Pick the workflow command for a URL when none was given on the CLI.

        Default: returns ``default_command``. Adaptors override this to
        route URL variants to different workflows (e.g. github/gitlab use
        URL fragments to pick ``respond`` over ``review`` automatically).
        """
        ...

    def matches(self, url: str) -> bool:
        """Return True if this adaptor handles the given URL."""
        ...

    def resolve_auth(self) -> str | None:
        """Resolve authentication token for this platform. Returns None if not configured."""
        ...

    def resolve_repo_url(
        self, issue_url: str, token: str, *, repo_override: str | None = None
    ) -> str | None:
        """Resolve a URL to a repository URL for cloning."""
        ...

    def fetch_sha(self, url: str, token: str) -> str | None:
        """Return the source SHA this URL needs, or None for default branch."""
        ...

    def fetch_head_ref(self, url: str, token: str) -> str | None:
        """Return the head branch name (e.g. ``feat/foo``) for a PR/MR URL.

        Returned alongside ``fetch_sha`` and used by ``clone_repos`` when
        the target skill needs a writable git tree (respond / fix). Adaptors
        whose URLs always target the default branch (sentry) return None.
        """
        ...

    def fetch_context(self, repo_dir: Path, url: str, token: str) -> None:
        """Populate <repo_dir>/.context/ with platform-specific data files."""
        ...

    def build_env(self, token: str, issue_urls: list[str]) -> dict[str, str]:
        """Build env vars the plugin skill needs for this platform."""
        ...

    def build_prompt(self, issue_urls: list[str]) -> str:
        """Build the user prompt to pass to the skill (typically just the URL(s))."""
        ...

    @property
    def display(self) -> Display:
        """Display logic for this platform's pipeline in the CLI."""
        ...
