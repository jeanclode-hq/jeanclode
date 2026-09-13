"""GitHub integration plugin.

Provides GitHub API functionality using BaseHttpPlugin.
Supports GitHub App installation authentication for repository access.
"""

import logging
import time
from pathlib import Path
from typing import Any

import jwt
from fastapi import HTTPException

from api.context import get_current_app
from api.plugins.container.backend import ContainerBackend
from api.plugins.container.docker import DockerBackend
from api.plugins.container.kubernetes import KubernetesBackend
from api.plugins.github.config import GitHubAppConfig, GitHubPluginConfig
from api.plugins.github.watcher import GitHubWatcher
from api.plugins.httpx.plugin import BaseHttpPlugin

logger = logging.getLogger(__name__)


class GitHubPlugin(BaseHttpPlugin[GitHubPluginConfig]):
    """GitHub integration plugin.

    Provides methods to interact with GitHub API for onboarding:
    - JWT generation for GitHub App authentication
    - Installation access token retrieval
    - Repository fetching for installations
    """

    plugin_name = "github"
    config_class = GitHubPluginConfig
    priority = 20

    def __init__(self, plugin_config: GitHubPluginConfig):
        """Initialize GitHub plugin."""
        super().__init__(plugin_config)
        # Env-derived private key (inline PEM or file path). Loaded once at
        # startup; DB-stored keys are resolved per-call in get_private_key().
        self._env_private_key: str | None = None
        self._watcher: GitHubWatcher | None = None

    async def startup(self) -> None:
        """Start HTTP service and load env-derived private key (if any)."""
        await super().startup()

        if self.config.app and self.config.app.private_key_pem:
            self._env_private_key = self.config.app.private_key_pem
            logger.info("Loaded GitHub App private key from inline PEM")
        elif self.config.app and self.config.app.private_key_path:
            private_key_path = Path(self.config.app.private_key_path)
            if not private_key_path.is_absolute() and not private_key_path.exists():
                parent_path = Path.cwd().parent / private_key_path
                if parent_path.exists():
                    private_key_path = parent_path

            if not private_key_path.exists():
                raise RuntimeError(
                    f"GitHub App private key not found at {private_key_path.resolve()}. "
                    f"Check GITHUB_APP_PRIVATE_KEY_PATH (current value: '{self.config.app.private_key_path}')."
                )

            self._env_private_key = private_key_path.read_text()
            logger.info(f"Loaded GitHub App private key from {private_key_path.resolve()}")

    async def watch(self) -> None:
        """Start the github container watcher.

        Called after all plugins have started, so FastStream and Container
        plugins are guaranteed ready.
        """
        if not self.config.watcher.enabled:
            return

        app = get_current_app()
        if not app.container or not app.faststream:
            logger.warning("Container or FastStream plugin not available, skipping github watcher")
            return

        broker = app.faststream.get_broker()
        redis = app.faststream.get_redis()

        backend: ContainerBackend
        if app.container.config.backend == "kubernetes":
            k8s_config = app.container.config.kubernetes
            if not k8s_config:
                logger.warning("Kubernetes config not available, skipping github watcher")
                return
            backend = KubernetesBackend("github", k8s_config, broker, redis)
        else:
            docker_config = app.container.config.docker
            if not docker_config:
                logger.warning("Docker config not available, skipping github watcher")
                return
            backend = DockerBackend("github", docker_config, broker, redis)

        self._watcher = GitHubWatcher(backend, self.config.watcher)
        await self._watcher.start()

    async def shutdown(self) -> None:
        """Stop watcher and HTTP service."""
        if self._watcher:
            await self._watcher.stop()
            self._watcher = None
        await super().shutdown()

    async def health_check(self) -> dict[str, Any]:
        """Check GitHub plugin health including watcher."""
        result: dict[str, Any] = {"healthy": True}
        if self._watcher:
            result["watcher"] = self._watcher.health_check()
            if not result["watcher"]["healthy"]:
                result["healthy"] = False
        return result

    # ------------------------------------------------------------------
    # Stateless effective-config accessors (multi-pod safe)
    # ------------------------------------------------------------------

    def _load_db_config(self) -> dict[str, str] | None:
        """Read the current DB-stored GitHub App config. ``None`` if absent."""
        app = get_current_app()
        if not app.database:
            return None
        # Local import to avoid import cycles at module load time.
        from api.services.instance_settings import load_github_config

        with app.database.session() as db:
            return load_github_config(db)

    def get_effective_app(self) -> GitHubAppConfig | None:
        """Return the effective App config (env-first, DB-fallback).

        Stateless — reads DB each call so peer pods always see fresh admin
        edits. Returns ``None`` if neither env nor DB provide a usable config.
        """
        env = self.config.app
        if env and env.client_id and env.client_secret and env.app_id and env.webhook_secret:
            return env  # env fully configured → skip DB

        db_cfg = self._load_db_config()
        if not db_cfg:
            return env  # may still be partial, but it's all we have

        # Merge: env wins per field, DB fills gaps.
        return GitHubAppConfig(
            client_id=(env.client_id if env and env.client_id else db_cfg.get("client_id", "")),
            client_secret=(
                env.client_secret if env and env.client_secret else db_cfg.get("client_secret", "")
            ),
            app_id=(env.app_id if env and env.app_id else db_cfg.get("app_id", "")),
            webhook_secret=(
                env.webhook_secret
                if env and env.webhook_secret
                else db_cfg.get("webhook_secret", "")
            ),
            name=(env.name if env and env.name else db_cfg.get("name", "")),
            authorize_url=env.authorize_url if env else "https://github.com/login/oauth/authorize",
            token_url=env.token_url if env else "https://github.com/login/oauth/access_token",
            api_base_url=env.api_base_url if env else "https://api.github.com/",
            scopes=env.scopes if env else ["read:user", "user:email", "read:org"],
            # Leave private-key fields empty on the returned model — callers
            # use get_private_key() explicitly. Avoids the mutually-exclusive
            # validator complaining when both env and DB supply a key.
            private_key_path=None,
            private_key_pem=None,
        )

    def get_private_key(self) -> str | None:
        """Return the effective private key (env wins, else DB-stored PEM)."""
        if self._env_private_key:
            return self._env_private_key
        db_cfg = self._load_db_config()
        if db_cfg and db_cfg.get("private_key_pem"):
            return db_cfg["private_key_pem"]
        return None

    def _get_base_url(self) -> str:
        """Get GitHub API base URL.

        Called from BaseHttpPlugin.__init__ before the app context is set, so
        it cannot touch the DB. ``api_base_url`` is env-only anyway (not in
        the admin-editable ``GITHUB_KEYS``).
        """
        if self.config.app and self.config.app.api_base_url:
            return self.config.app.api_base_url
        return "https://api.github.com/"

    def _get_default_headers(self) -> dict[str, str]:
        """Get default headers for GitHub API."""
        return {
            "Accept": "application/vnd.github.v3+json",
        }

    def _generate_jwt(self) -> str:
        """Generate a JWT for GitHub App authentication.

        Returns:
            JWT token string

        Raises:
            HTTPException: If private key not loaded or JWT generation fails
        """
        private_key = self.get_private_key()
        if not private_key:
            raise HTTPException(
                status_code=500,
                detail="GitHub App private key not loaded.",
            )

        effective_app = self.get_effective_app()
        if not effective_app or not effective_app.app_id:
            raise HTTPException(status_code=500, detail="GitHub App configuration not found")

        now = int(time.time())
        expiration = now + (10 * 60)

        payload = {
            "iat": now,
            "exp": expiration,
            "iss": effective_app.app_id,
        }

        try:
            token = jwt.encode(payload, private_key, algorithm="RS256")
            return token
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to generate GitHub App JWT: {e}"
            ) from e

    async def get_installation_access_token(self, installation_id: str) -> str:
        """Get an installation access token for GitHub App.

        Args:
            installation_id: GitHub App installation ID

        Returns:
            Installation access token string

        Raises:
            HTTPException: If token generation fails
        """
        jwt_token = self._generate_jwt()

        url = f"/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            response = await self.http.post(url, headers=headers)

            if response.status_code != 201:
                raise HTTPException(
                    status_code=response.status_code, detail=f"GitHub API error: {response.text}"
                )

            data = response.json()
            return data["token"]

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to get installation access token: {e}"
            ) from e

    async def delete_installation(self, installation_id: str) -> None:
        """Delete a GitHub App installation.

        Args:
            installation_id: GitHub App installation ID

        Raises:
            HTTPException: If deletion fails
        """
        jwt_token = self._generate_jwt()

        url = f"/app/installations/{installation_id}"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            response = await self.http.delete(url, headers=headers)

            # 204 = success, 404 = already deleted
            if response.status_code not in (204, 404):
                raise HTTPException(
                    status_code=response.status_code, detail=f"GitHub API error: {response.text}"
                )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to delete installation: {e}"
            ) from e

    async def fetch_user_info(self, access_token: str) -> dict:
        """Fetch the authenticated user's profile from GitHub.

        Args:
            access_token: User's OAuth access token

        Returns:
            User profile dict with id, login, email, avatar_url, etc.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        response = await self.http.get("/user", headers=headers)
        response.raise_for_status()
        return response.json()

    async def fetch_user_emails(self, access_token: str) -> list[dict]:
        """Fetch the authenticated user's email addresses from GitHub.

        Args:
            access_token: User's OAuth access token

        Returns:
            List of email objects with email, primary, verified fields.
        """
        headers = {"Authorization": f"Bearer {access_token}"}
        response = await self.http.get("/user/emails", headers=headers)
        if response.status_code != 200:
            return []
        return response.json()

    async def fetch_installation_repositories(self, installation_token: str) -> list[dict]:
        """Fetch all repositories accessible by an installation.

        Args:
            installation_token: GitHub App installation access token

        Returns:
            List of repository objects

        Raises:
            HTTPException: If API call fails
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        try:
            response = await self.http.get("/installation/repositories", headers=headers)

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code, detail=f"GitHub API error: {response.text}"
                )

            data = response.json()
            return data.get("repositories", [])

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch installation repositories: {e}"
            ) from e

    async def fetch_repository_by_id(
        self, installation_token: str, external_id: str
    ) -> dict[str, Any] | None:
        """Fetch a single repository's metadata by its numeric GitHub id.

        Used for live disk-size lookups (the ``size`` field, in KB) —
        ``external_id`` is the id installation syncs already store on
        ``Repository``, so no owner/repo URL parsing is needed.
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await self.http.get(f"/repositories/{external_id}", headers=headers)
        if response.status_code != 200:
            logger.warning("Failed to fetch repository %s: %s", external_id, response.status_code)
            return None
        return response.json()

    async def fetch_repo_tree_bytes(
        self, installation_token: str, external_id: str, default_branch: str
    ) -> int | None:
        """Total bytes of every blob at ``default_branch`` — a checkout's size.

        The ``size`` field on the repo is the packed object store and badly
        understates a checkout of compressible content. GitHub's tree API
        returns a per-blob ``size``, so one recursive call prices the whole
        working tree. Returns ``None`` when the tree is unusable — including
        GitHub's 100k-entry truncation, where the sum would silently be short.
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await self.http.get(
            f"/repositories/{external_id}/git/trees/{default_branch}",
            headers=headers,
            params={"recursive": "1"},
        )
        if response.status_code != 200:
            logger.warning(
                "tree listing failed for repository %s: %s", external_id, response.status_code
            )
            return None
        payload = response.json()
        if payload.get("truncated"):
            logger.warning("tree truncated for repository %s, size would undercount", external_id)
            return None
        return sum(
            int(e.get("size") or 0) for e in payload.get("tree", []) if e.get("type") == "blob"
        )

    async def fetch_repo_file_text(
        self,
        owner: str,
        repo: str,
        file_path: str,
        *,
        ref: str | None = None,
        auth_token: str | None = None,
    ) -> tuple[int, str]:
        """Fetch a raw file's text from a GitHub repo via the contents API.

        Works for both public repos (no auth) and private repos (Bearer
        ``auth_token``). Returns ``(status_code, body_text)`` so callers can
        translate domain-specific 404/403 outcomes themselves.
        """
        from urllib.parse import quote

        path_q = quote(file_path)
        url = f"/repos/{owner}/{repo}/contents/{path_q}"
        if ref:
            url = f"{url}?ref={quote(ref)}"

        headers = {"Accept": "application/vnd.github.raw+json"}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        response = await self.http.get(url, headers=headers)
        return response.status_code, response.text

    async def fetch_repository_pull_requests(
        self, installation_token: str, owner: str, repo: str, state: str = "open"
    ) -> list[dict]:
        """Fetch pull requests for a repository.

        Args:
            installation_token: GitHub App installation access token
            owner: Repository owner (org or user)
            repo: Repository name
            state: PR state filter (open, closed, all). Defaults to open.

        Returns:
            List of pull request objects
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        pull_requests: list[dict] = []
        page = 1
        per_page = 100

        while True:
            response = await self.http.get(
                f"/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"state": state, "page": page, "per_page": per_page},
            )

            if response.status_code != 200:
                logger.warning(f"Failed to fetch PRs for {owner}/{repo}: {response.status_code}")
                break

            page_prs = response.json()
            if not page_prs:
                break

            pull_requests.extend(page_prs)
            page += 1
        return pull_requests

    async def fetch_pull_request(
        self,
        installation_token: str,
        owner: str,
        repo: str,
        pr_number: int,
    ) -> dict | None:
        """Fetch a single PR's current state (Part E1 reconciliation).

        Returns the PR object, ``None`` on a transient failure (network,
        rate limit, 5xx) so the caller falls back to the last-known DB
        state, or ``{"state": "closed", "_deleted": True}`` on a definitive
        404 — a deleted PR is closed as far as the gate is concerned.
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        try:
            response = await self.http.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}", headers=headers
            )
        except Exception:
            logger.warning("fetch_pull_request %s/%s#%d errored", owner, repo, pr_number)
            return None
        if response.status_code == 404:
            return {"state": "closed", "merged": False, "_deleted": True}
        if response.status_code != 200:
            logger.warning(
                "fetch_pull_request %s/%s#%d: %s", owner, repo, pr_number, response.status_code
            )
            return None
        return response.json()

    async def close_and_scrub_pull_request(
        self,
        installation_token: str,
        owner: str,
        repo: str,
        pr_number: int,
        *,
        title: str,
        body: str,
    ) -> None:
        """Close a PR and overwrite its title/body in one call (ADR-010).

        Used by the rate-limit watcher to clean up a leftover ``sentry_fix``
        draft PR before a full-workflow retry re-triages: the PR's body
        carries the fingerprint (Sentry issue URLs/root-cause text) that
        ``TriageAgent``'s ``gh pr list --state all`` search would otherwise
        match, silently treating the issue as already handled.
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await self.http.patch(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            headers=headers,
            json={"state": "closed", "title": title, "body": body},
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to close/scrub PR %s/%s#%d: %s",
                owner,
                repo,
                pr_number,
                response.status_code,
            )

    async def delete_branch(
        self,
        installation_token: str,
        owner: str,
        repo: str,
        branch: str,
    ) -> None:
        """Delete a branch. Best-effort hygiene — not load-bearing, since a
        future ``push --force`` to the same branch would overwrite it anyway."""
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await self.http.delete(
            f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=headers,
        )
        if response.status_code not in (204, 200, 422):
            logger.warning(
                "Failed to delete branch %s on %s/%s: %s",
                branch,
                owner,
                repo,
                response.status_code,
            )

    async def fetch_repository_issues(
        self, installation_token: str, owner: str, repo: str, state: str = "open"
    ) -> list[dict]:
        """Fetch issues for a repository.

        Args:
            installation_token: GitHub App installation access token
            owner: Repository owner (org or user)
            repo: Repository name
            state: Issue state filter (open, closed, all). Defaults to open.

        Returns:
            List of issue objects (excludes pull requests)
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        issues: list[dict] = []
        page = 1
        per_page = 100

        while True:
            response = await self.http.get(
                f"/repos/{owner}/{repo}/issues",
                headers=headers,
                params={"state": state, "page": page, "per_page": per_page},
            )

            if response.status_code != 200:
                logger.warning(f"Failed to fetch issues for {owner}/{repo}: {response.status_code}")
                break

            page_issues = response.json()
            if not page_issues:
                break

            # GitHub's issues endpoint includes PRs — filter them out
            for issue in page_issues:
                if "pull_request" not in issue:
                    issues.append(issue)

            page += 1

        return issues

    async def list_issue_comments(
        self, installation_token: str, owner: str, repo: str, number: int
    ) -> list[dict]:
        """List comments on a PR or issue (GitHub treats both as issues).

        Used to find an existing sticky status comment by marker before
        deciding whether to create or update.
        """
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        comments: list[dict] = []
        page = 1
        per_page = 100
        while True:
            response = await self.http.get(
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                headers=headers,
                params={"page": page, "per_page": per_page},
            )
            if response.status_code != 200:
                logger.warning(
                    "Failed to list comments for %s/%s#%s: %s",
                    owner,
                    repo,
                    number,
                    response.status_code,
                )
                break
            page_comments = response.json()
            if not page_comments:
                break
            comments.extend(page_comments)
            page += 1
        return comments

    async def create_issue_comment(
        self, installation_token: str, owner: str, repo: str, number: int, body: str
    ) -> None:
        """Post a new comment on a PR or issue."""
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await self.http.post(
            f"/repos/{owner}/{repo}/issues/{number}/comments",
            headers=headers,
            json={"body": body},
        )
        if response.status_code != 201:
            logger.warning(
                "Failed to create comment on %s/%s#%s: %s",
                owner,
                repo,
                number,
                response.status_code,
            )

    async def update_issue_comment(
        self, installation_token: str, owner: str, repo: str, comment_id: int, body: str
    ) -> None:
        """Edit an existing comment in place."""
        headers = {
            "Authorization": f"token {installation_token}",
            "Accept": "application/vnd.github.v3+json",
        }
        response = await self.http.patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            headers=headers,
            json={"body": body},
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to update comment %s on %s/%s: %s",
                comment_id,
                owner,
                repo,
                response.status_code,
            )
