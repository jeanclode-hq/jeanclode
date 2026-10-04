"""An in-memory stand-in for the GitHub and GitLab plugins' repo-reading calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock


def skill_md(name: str, description: str = "does things") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\nbody\n"


class FakeRepos:
    """Files per ``(provider, project_path, ref)``; ``None`` ref is the default branch."""

    def __init__(self, gitlab_instance: str = "https://gitlab.example.org") -> None:
        self.files: dict[tuple[str, str, str | None], dict[str, str]] = {}
        self.status: dict[tuple[str, str], int] = {}
        self.truncated = False
        self.github = SimpleNamespace(
            fetch_repo_file_text=AsyncMock(side_effect=self._gh_file),
            list_repo_blob_paths=AsyncMock(side_effect=self._gh_tree),
        )
        self.gitlab = SimpleNamespace(
            fetch_repo_file_text=AsyncMock(side_effect=self._gl_file),
            list_repo_blob_paths=AsyncMock(side_effect=self._gl_tree),
            get_effective_instance_url=MagicMock(return_value=gitlab_instance),
        )

    def add(self, provider: str, path: str, files: dict[str, str], ref: str | None = None) -> None:
        self.files[(provider, path, ref)] = files

    def _repo(self, provider: str, path: str, ref: str | None) -> dict[str, str] | None:
        return self.files.get((provider, path, ref))

    def _file(self, provider, path, file_path, ref):
        repo = self._repo(provider, path, ref)
        if repo is None:
            return 404, '{"message":"404 Project Not Found"}'
        if file_path not in repo:
            return 404, '{"message":"404 File Not Found"}'
        return 200, repo[file_path]

    def _tree(self, provider, path, ref, subpath=None):
        if (forced := self.status.get((provider, "tree"))) is not None:
            return forced, [], False
        repo = self._repo(provider, path, ref)
        if repo is None:
            return 404, [], False
        paths = [p for p in repo if not subpath or p.startswith(f"{subpath}/")]
        return 200, paths, self.truncated

    async def _gh_file(self, owner, repo, file_path, *, ref=None, auth_token=None):
        return self._file("github", f"{owner}/{repo}", file_path, ref)

    async def _gh_tree(self, owner, repo, *, ref=None, auth_token=None):
        return self._tree("github", f"{owner}/{repo}", ref)

    async def _gl_file(
        self, project_path, file_path, *, ref=None, auth_token=None, provider_url=None
    ):
        return self._file("gitlab", project_path, file_path, ref)

    async def _gl_tree(
        self, project_path, *, ref=None, path=None, auth_token=None, provider_url=None, max_pages=50
    ):
        return self._tree("gitlab", project_path, ref, path)
