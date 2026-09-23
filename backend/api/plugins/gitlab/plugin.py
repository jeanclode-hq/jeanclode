"""GitLab integration plugin.

Provides GitLab API functionality using BaseHttpPlugin.
"""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from api.models.repositories import Repository

from fastapi import HTTPException

from api.context import get_current_app
from api.plugins.container.backend import ContainerBackend
from api.plugins.container.docker import DockerBackend
from api.plugins.container.kubernetes import KubernetesBackend
from api.plugins.gitlab.config import GitLabOAuthConfig, GitLabPluginConfig
from api.plugins.gitlab.dispatch import LABEL_RESOLVE, LABEL_REVIEW, LABEL_SUMMARY
from api.plugins.gitlab.schemas import GitlabTokenType
from api.plugins.gitlab.watcher import GitLabWatcher
from api.plugins.httpx.plugin import BaseHttpPlugin

logger = logging.getLogger(__name__)

# GitLab group labels are inherited by every subgroup/project underneath, so
# these only need creating once per connected group.
_TRIGGER_LABELS: dict[str, tuple[str, str]] = {
    LABEL_REVIEW: ("#0E8A16", "Jeanclode runs a code review on this merge request"),
    LABEL_SUMMARY: ("#1D76DB", "Jeanclode writes this merge request's description"),
    LABEL_RESOLVE: ("#5319E7", "Jeanclode attempts a fix for this issue"),
}


class GitLabPlugin(BaseHttpPlugin[GitLabPluginConfig]):
    """GitLab integration plugin.

    Provides methods to interact with GitLab API for onboarding:
    - Token verification and type detection
    - Group and project fetching
    """

    plugin_name = "gitlab"
    config_class = GitLabPluginConfig
    priority = 20

    def __init__(self, plugin_config: GitLabPluginConfig):
        super().__init__(plugin_config)
        self._watcher: GitLabWatcher | None = None

    async def watch(self) -> None:
        """Start the gitlab container watcher.

        Called after all plugins have started, so FastStream and Container
        plugins are guaranteed ready.
        """
        if not self.config.watcher.enabled:
            return

        app = get_current_app()
        if not app.container or not app.faststream:
            logger.warning("Container or FastStream plugin not available, skipping gitlab watcher")
            return

        broker = app.faststream.get_broker()
        redis = app.faststream.get_redis()

        backend: ContainerBackend
        if app.container.config.backend == "kubernetes":
            k8s_config = app.container.config.kubernetes
            if not k8s_config:
                logger.warning("Kubernetes config not available, skipping gitlab watcher")
                return
            backend = KubernetesBackend("gitlab", k8s_config, broker, redis)
        else:
            docker_config = app.container.config.docker
            if not docker_config:
                logger.warning("Docker config not available, skipping gitlab watcher")
                return
            backend = DockerBackend("gitlab", docker_config, broker, redis)

        self._watcher = GitLabWatcher(backend, self.config.watcher)
        await self._watcher.start()

    async def shutdown(self) -> None:
        """Stop watcher and HTTP service."""
        if self._watcher:
            await self._watcher.stop()
            self._watcher = None
        await super().shutdown()

    async def health_check(self) -> dict[str, Any]:
        """Check GitLab plugin health including watcher."""
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
        """Read the current DB-stored GitLab config. ``None`` if absent."""
        app = get_current_app()
        if not app.database:
            return None
        # Local import to avoid import cycles at module load time.
        from api.services.instance_settings import load_gitlab_config

        with app.database.session() as db:
            return load_gitlab_config(db)

    def get_effective_oauth(self) -> GitLabOAuthConfig | None:
        """Return the effective OAuth config (env-first, DB-fallback).

        Stateless — reads DB each call so peer pods always see fresh admin
        edits. Returns ``None`` if neither env nor DB provide a usable config.
        """
        env = self.config.oauth
        if env and env.client_id and env.client_secret:
            return env  # env fully configured → skip DB

        db_cfg = self._load_db_config()
        if not db_cfg:
            return env  # may still be partial, but it's all we have

        # Merge: env wins per field, DB fills gaps.
        return GitLabOAuthConfig(
            client_id=(env.client_id if env and env.client_id else db_cfg.get("client_id", "")),
            client_secret=(
                env.client_secret if env and env.client_secret else db_cfg.get("client_secret", "")
            ),
            instance_url=(
                env.instance_url
                if env and env.client_id and env.instance_url
                else db_cfg.get("instance_url", "https://gitlab.com")
            ),
            scopes=env.scopes if env else ["read_user", "read_api"],
        )

    def get_effective_webhook_secret(self) -> str | None:
        """Return the effective webhook secret (env wins, else DB)."""
        if self.config.webhook_secret:
            return self.config.webhook_secret
        db_cfg = self._load_db_config()
        if db_cfg and db_cfg.get("webhook_secret"):
            return db_cfg["webhook_secret"]
        return None

    def get_effective_webhook_url(self) -> str | None:
        """Return the public URL GitLab should call, e.g.
        ``https://jeanclode.example.com/webhooks/gitlab``.

        Derived from the backend's own public base URL (``BACKEND_URL``);
        ``gitlab.webhook_url`` is a full-endpoint override for when it lives
        elsewhere. Only used to create project webhooks for
        ``manage_project_webhooks`` orgs — incoming deliveries are matched on
        the secret token, not the URL.
        """
        if self.config.webhook_url:
            return self.config.webhook_url.rstrip("/")
        base = get_current_app().options.backend_url
        return f"{base.rstrip('/')}/webhooks/gitlab" if base else None

    def get_effective_instance_url(self) -> str:
        """Return the effective GitLab instance URL (env-first, DB-fallback)."""
        oauth = self.get_effective_oauth()
        return oauth.instance_url if oauth and oauth.instance_url else "https://gitlab.com"

    def _get_base_url(self) -> str:
        """Get GitLab API base URL.

        Called from BaseHttpPlugin.__init__ before the app context is set, so
        it cannot touch the DB. This is only a fallback for relative-path
        calls; every API method in this plugin resolves the effective
        instance URL and passes an absolute URL to ``self.http`` anyway.
        """
        env = self.config.oauth
        instance_url = env.instance_url if env and env.instance_url else "https://gitlab.com"
        return f"{instance_url}/api/v4"

    def _get_default_headers(self) -> dict[str, str]:
        """Get default headers for GitLab API."""
        return {}

    async def verify_token(self, access_token: str, provider_url: str | None = None) -> dict:
        """Verify GitLab access token and get user information.

        Args:
            access_token: GitLab token (group, project, or personal)
            provider_url: Optional GitLab instance URL (defaults to config)

        Returns:
            User information dict

        Raises:
            HTTPException: If token is invalid or API call fails
        """
        base_url = provider_url or self.get_effective_instance_url()
        response = await self.http.get(
            f"{base_url}/api/v4/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code == 401:
            raise HTTPException(
                status_code=401,
                detail="Invalid GitLab access token",
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {response.status_code}: {response.text}",
            )

        return response.json()

    def get_token_type(self, user_data: dict) -> GitlabTokenType | None:
        """Determine the type of token from user data.

        Args:
            user_data: User data from /api/v4/user endpoint

        Returns:
            Token type or None if unrecognized
        """
        is_bot = user_data.get("bot", False)
        username = user_data.get("username", "")

        if not is_bot:
            return GitlabTokenType.PERSONAL_ACCESS_TOKEN

        if username.startswith("group_") and "_bot_" in username:
            return GitlabTokenType.GROUP_ACCESS_TOKEN

        if username.startswith("project_") and "_bot_" in username:
            return GitlabTokenType.PROJECT_ACCESS_TOKEN

        return None

    async def fetch_group(
        self, access_token: str, group_id: str, provider_url: str | None = None
    ) -> dict:
        """Fetch GitLab group information.

        Args:
            access_token: GitLab access token
            group_id: Group ID
            provider_url: Optional GitLab instance URL (defaults to config)

        Returns:
            Group information

        Raises:
            HTTPException: If API call fails
        """
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.get(
            f"{instance_url}/api/v4/groups/{group_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {response.status_code}: {response.text}",
            )

        return response.json()

    async def fetch_group_projects(
        self, access_token: str, group_id: str, provider_url: str | None = None
    ) -> list[dict]:
        """Fetch all projects in a GitLab group.

        Args:
            access_token: GitLab access token
            group_id: Group ID
            provider_url: Optional GitLab instance URL (defaults to config)

        Returns:
            List of project information

        Raises:
            HTTPException: If API call fails
        """
        instance_url = provider_url or self.get_effective_instance_url()
        projects = []
        page = 1
        per_page = 100

        while True:
            response = await self.http.get(
                f"{instance_url}/api/v4/groups/{group_id}/projects",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "page": page,
                    "per_page": per_page,
                    "include_subgroups": True,
                    "with_shared": False,
                },
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=500,
                    detail=f"GitLab API returned {response.status_code}: {response.text}",
                )

            page_projects = response.json()
            if not page_projects:
                break

            projects.extend(page_projects)
            page += 1

        return projects

    async def fetch_project_merge_requests(
        self,
        access_token: str,
        project_id: str,
        provider_url: str | None = None,
        state: str = "opened",
    ) -> list[dict]:
        """Fetch merge requests for a GitLab project.

        Args:
            access_token: GitLab access token
            project_id: Project ID
            provider_url: Optional GitLab instance URL
            state: MR state filter (opened, closed, merged, all). Defaults to opened.

        Returns:
            List of merge request objects
        """
        instance_url = provider_url or self.get_effective_instance_url()
        merge_requests: list[dict] = []
        page = 1
        per_page = 100

        while True:
            response = await self.http.get(
                f"{instance_url}/api/v4/projects/{project_id}/merge_requests",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"state": state, "page": page, "per_page": per_page},
            )

            if response.status_code != 200:
                logger.warning(
                    f"Failed to fetch MRs for project {project_id}: {response.status_code}"
                )
                break

            page_mrs = response.json()
            if not page_mrs:
                break

            merge_requests.extend(page_mrs)
            page += 1

        return merge_requests

    async def fetch_merge_request(
        self,
        access_token: str,
        project_id: str,
        mr_iid: int,
        provider_url: str | None = None,
    ) -> dict | None:
        """Fetch a single MR's current state (Part E1 reconciliation).

        Returns the MR object, ``None`` on a transient failure so the caller
        keeps the last-known DB state, or a synthetic closed object on a
        definitive 404 (the MR was deleted).
        """
        instance_url = provider_url or self.get_effective_instance_url()
        try:
            response = await self.http.get(
                f"{instance_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except Exception:
            logger.warning("fetch_merge_request %s!%d errored", project_id, mr_iid)
            return None
        if response.status_code == 404:
            return {"state": "closed", "_deleted": True}
        if response.status_code != 200:
            logger.warning(
                "fetch_merge_request %s!%d: %s", project_id, mr_iid, response.status_code
            )
            return None
        return response.json()

    async def close_and_scrub_merge_request(
        self,
        access_token: str,
        project_id: str,
        mr_iid: int,
        *,
        title: str,
        description: str,
        provider_url: str | None = None,
    ) -> None:
        """Close an MR and overwrite its title/description in one call (ADR-010).

        Used by the rate-limit watcher to clean up a leftover ``sentry_fix``
        draft MR before a full-workflow retry re-triages — see
        ``GitHubPlugin.close_and_scrub_pull_request`` for why this matters.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.put(
            f"{instance_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"state_event": "close", "title": title, "description": description},
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to close/scrub MR %s!%d: %s",
                project_id,
                mr_iid,
                response.status_code,
            )

    async def delete_branch(
        self,
        access_token: str,
        project_id: str,
        branch: str,
        provider_url: str | None = None,
    ) -> None:
        """Delete a branch. Best-effort hygiene — not load-bearing, since a
        future ``push --force`` to the same branch would overwrite it anyway."""
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.delete(
            f"{instance_url}/api/v4/projects/{project_id}/repository/branches/{branch}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code not in (204, 200, 404):
            logger.warning(
                "Failed to delete branch %s on project %s: %s",
                branch,
                project_id,
                response.status_code,
            )

    async def fetch_project_issues(
        self,
        access_token: str,
        project_id: str,
        provider_url: str | None = None,
        state: str = "opened",
    ) -> list[dict]:
        """Fetch issues for a GitLab project.

        Args:
            access_token: GitLab access token
            project_id: Project ID
            provider_url: Optional GitLab instance URL
            state: Issue state filter (opened, closed, all). Defaults to opened.

        Returns:
            List of issue objects
        """
        instance_url = provider_url or self.get_effective_instance_url()
        issues: list[dict] = []
        page = 1
        per_page = 100

        while True:
            response = await self.http.get(
                f"{instance_url}/api/v4/projects/{project_id}/issues",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"state": state, "page": page, "per_page": per_page},
            )

            if response.status_code != 200:
                logger.warning(
                    f"Failed to fetch issues for project {project_id}: {response.status_code}"
                )
                break

            page_issues = response.json()
            if not page_issues:
                break

            issues.extend(page_issues)
            page += 1

        return issues

    async def fetch_repo_file_text(
        self,
        project_path: str,
        file_path: str,
        *,
        ref: str | None = None,
        auth_token: str | None = None,
        provider_url: str | None = None,
    ) -> tuple[int, str]:
        """Fetch a raw file's text from a GitLab repo via the projects API.

        ``project_path`` is the URL-encodable namespace path (``group/repo``).
        Returns ``(status_code, body_text)`` so callers can translate
        domain-specific 404/403 outcomes themselves.
        """
        from urllib.parse import quote

        instance_url = provider_url or self.get_effective_instance_url()
        project_q = quote(project_path, safe="")
        path_q = quote(file_path, safe="")
        ref_q = quote(ref) if ref else "HEAD"
        url = (
            f"{instance_url}/api/v4/projects/{project_q}/repository/files/{path_q}/raw?ref={ref_q}"
        )

        headers: dict[str, str] = {}
        if auth_token:
            headers["PRIVATE-TOKEN"] = auth_token

        response = await self.http.get(url, headers=headers)
        return response.status_code, response.text

    async def fetch_group_ancestors(
        self,
        access_token: str,
        group_id: str,
        provider_url: str | None = None,
        known_parent_id: int | str | None = None,
    ) -> list[dict]:
        """Fetch ancestor chain for a GitLab group by walking parent_id upward.

        GitLab has no dedicated ancestors endpoint; each GET /groups/:id response
        contains a ``parent_id`` field that points to the immediate parent.  This
        method walks the chain iteratively until it reaches a top-level group
        (``parent_id == null``).

        Args:
            access_token: GitLab access token
            group_id: Starting group ID (the group whose ancestors we want)
            provider_url: Optional GitLab instance URL (defaults to config)
            known_parent_id: Caller-supplied parent_id from an already-fetched
                group response, avoids a redundant GET /groups/{group_id} call.

        Returns:
            List of ancestor group dicts ordered from immediate parent to root.
            Empty list for top-level groups (no ancestors).
        """
        instance_url = provider_url or self.get_effective_instance_url()
        headers = {"Authorization": f"Bearer {access_token}"}
        ancestors: list[dict] = []
        seen: set[str] = set()

        if known_parent_id is not None:
            current_parent_id: int | str | None = known_parent_id
        else:
            response = await self.http.get(
                f"{instance_url}/api/v4/groups/{group_id}",
                headers=headers,
            )
            if response.status_code != 200:
                logger.warning(
                    f"Could not fetch group {group_id} for ancestor resolution: "
                    f"{response.status_code}"
                )
                return []
            current_parent_id = response.json().get("parent_id")

        while current_parent_id is not None:
            pid = str(current_parent_id)
            if pid in seen:
                break
            seen.add(pid)

            resp = await self.http.get(
                f"{instance_url}/api/v4/groups/{pid}",
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning(f"Could not fetch ancestor group {pid}: {resp.status_code}")
                break

            parent_data = resp.json()
            ancestors.append(parent_data)
            current_parent_id = parent_data.get("parent_id")

        return ancestors

    def resolve_repo_token(
        self,
        db: Session,
        repo: Repository,
    ) -> tuple[str | None, str]:
        """Resolve the effective encrypted auth token for a repo.

        Resolution order:
        1. ``repo.auth_token_encrypted`` — project token stored on the repo row
        2. ``org.auth_token_encrypted`` — group/subgroup token on the direct org
        3. Walk up ``parent_org_id`` chain until a non-null token is found

        Returns:
            ``(encrypted_token, base_url)`` where ``encrypted_token`` may be ``None``
            if no token is found anywhere in the hierarchy.
        """
        from api.database.organization import db_get_org_by_id

        if repo.auth_token_encrypted:
            return repo.auth_token_encrypted, repo.provider_url or "https://gitlab.com"

        org = db_get_org_by_id(db, repo.org_id)
        while org:
            if org.auth_token_encrypted:
                return org.auth_token_encrypted, org.base_url or "https://gitlab.com"
            if not org.parent_org_id:
                break
            org = db_get_org_by_id(db, org.parent_org_id)

        return None, "https://gitlab.com"

    async def fetch_group_member(
        self,
        access_token: str,
        group_id: str,
        gitlab_user_id: str,
        provider_url: str | None = None,
    ) -> dict | None:
        """Check if a user is a member of a GitLab group (includes inherited members).

        Returns the member dict containing access_level, or None if not a member.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.get(
            f"{instance_url}/api/v4/groups/{group_id}/members/all",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"user_id": gitlab_user_id},
        )
        if response.status_code != 200:
            logger.warning(
                f"groups/{group_id}/members/all returned {response.status_code} "
                f"for user {gitlab_user_id}"
            )
            return None
        members = response.json()
        return members[0] if members else None

    async def fetch_project_member(
        self,
        access_token: str,
        project_id: str,
        gitlab_user_id: str,
        provider_url: str | None = None,
    ) -> dict | None:
        """Check if a user is a member of a GitLab project (includes inherited members).

        Returns the member dict containing access_level, or None if not a member.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.get(
            f"{instance_url}/api/v4/projects/{project_id}/members/all",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"user_id": gitlab_user_id},
        )
        if response.status_code != 200:
            logger.warning(
                f"projects/{project_id}/members/all returned {response.status_code} "
                f"for user {gitlab_user_id}"
            )
            return None
        members = response.json()
        return members[0] if members else None

    async def fetch_project(
        self,
        access_token: str,
        project_id: str,
        provider_url: str | None = None,
        *,
        statistics: bool = False,
    ) -> dict:
        """Fetch a single GitLab project.

        Args:
            access_token: GitLab access token
            project_id: Project ID
            provider_url: Optional GitLab instance URL (defaults to config)
            statistics: Include the ``statistics`` block (repository_size,
                storage_size, …) — used for live disk-size lookups.

        Returns:
            Project information

        Raises:
            HTTPException: If API call fails
        """
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.get(
            f"{instance_url}/api/v4/projects/{project_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"statistics": "true"} if statistics else None,
        )

        if response.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {response.status_code}: {response.text}",
            )

        return response.json()

    async def fetch_project_tree_bytes(
        self,
        access_token: str,
        project_id: str,
        full_path: str,
        provider_url: str | None = None,
        *,
        ref: str = "HEAD",
    ) -> int | None:
        """Total bytes of every blob at ``ref`` — the size of a checkout.

        ``statistics.repository_size`` measures the packed object store, which
        for compressible content understates the working tree by more than an
        order of magnitude (a 40Mi repo of load-test JSON checks out at 1.3Gi).
        This sums the blobs instead: paths from the REST tree, sizes from
        GraphQL ``rawSize``. Both fan out — the pages of the tree listing and
        the batches of paths are independent.

        Returns ``None`` when the tree can't be listed, so callers can tell
        "no data" from a genuinely empty repo.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        headers = {"Authorization": f"Bearer {access_token}"}
        tree_url = (
            f"{instance_url}/api/v4/projects/{project_id}"
            f"/repository/tree?recursive=true&per_page=100&ref={ref}"
        )

        first = await self.http.get(f"{tree_url}&page=1", headers=headers)
        if first.status_code != 200:
            logger.warning("tree listing failed for project %s: %s", project_id, first.status_code)
            return None

        def blobs_in(payload: Any) -> list[str]:
            return [e["path"] for e in payload if e.get("type") == "blob"]

        paths = blobs_in(first.json())
        total_pages = first.headers.get("X-Total-Pages")
        if total_pages:
            rest = await asyncio.gather(
                *(
                    self.http.get(f"{tree_url}&page={p}", headers=headers)
                    for p in range(2, int(total_pages) + 1)
                ),
                return_exceptions=True,
            )
            for r in rest:
                if isinstance(r, BaseException) or r.status_code != 200:
                    logger.warning("tree page failed for project %s, size undercounts", project_id)
                    return None
                paths += blobs_in(r.json())
        else:
            # GitLab drops X-Total-Pages past 10k items and switches to keyset
            # paging. Walking it serially is slow but correct; sizing off page
            # one alone would silently undercount a huge repo.
            page = first.headers.get("X-Next-Page")
            while page:
                r = await self.http.get(f"{tree_url}&page={page}", headers=headers)
                if r.status_code != 200:
                    logger.warning("tree page failed for project %s, size undercounts", project_id)
                    return None
                paths += blobs_in(r.json())
                page = r.headers.get("X-Next-Page")

        if not paths:
            return 0

        async def batch(chunk: list[str]) -> int | None:
            query = (
                f'{{ project(fullPath: "{full_path}") {{ repository '
                f"{{ blobs(paths: {json.dumps(chunk)}) {{ nodes {{ rawSize }} }} }} }} }}"
            )
            r = await self.http.post(
                f"{instance_url}/api/graphql", json={"query": query}, headers=headers
            )
            if r.status_code != 200:
                return None
            body = r.json()
            if "errors" in body:
                logger.warning("blob size query failed for %s: %s", full_path, body["errors"])
                return None
            project = (body.get("data") or {}).get("project") or {}
            nodes = ((project.get("repository") or {}).get("blobs") or {}).get("nodes") or []
            return sum(int(n["rawSize"] or 0) for n in nodes)

        results = await asyncio.gather(
            *(batch(paths[i : i + 100]) for i in range(0, len(paths), 100)),
            return_exceptions=True,
        )
        total = 0
        for r in results:
            if isinstance(r, BaseException) or r is None:
                logger.warning("blob size batch failed for %s, size undercounts", full_path)
                return None
            total += r
        return total

    async def ensure_project_webhook(
        self,
        access_token: str,
        project_id: str,
        *,
        hook_url: str,
        secret: str,
        provider_url: str | None = None,
    ) -> str:
        """Make sure the Jeanclode webhook exists on a project.

        Matches an existing hook by URL so a re-sync doesn't stack duplicates,
        and PUTs it back into shape if the tenant edited its events. Returns
        ``"created"``, ``"updated"`` or ``"exists"``.

        Needs a token with ``api`` scope and Maintainer+ role — GitLab answers
        403 otherwise, which surfaces here as an HTTPException.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        headers = {"Authorization": f"Bearer {access_token}"}
        base = f"{instance_url}/api/v4/projects/{project_id}/hooks"
        desired = {
            "url": hook_url,
            "token": secret,
            "issues_events": True,
            "confidential_issues_events": True,
            "merge_requests_events": True,
            "note_events": True,
            "confidential_note_events": True,
            "push_events": False,
            "enable_ssl_verification": instance_url.startswith("https://"),
        }
        # Events that must be on — used to detect a tenant-edited hook.
        required_flags = (
            "issues_events",
            "confidential_issues_events",
            "merge_requests_events",
            "note_events",
            "confidential_note_events",
        )

        resp = await self.http.get(base, headers=headers, params={"per_page": 100})
        if resp.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {resp.status_code} listing hooks: {resp.text}",
            )
        existing = next((h for h in resp.json() if h.get("url") == hook_url), None)

        if existing is None:
            created = await self.http.post(base, headers=headers, json=desired)
            if created.status_code not in (200, 201):
                raise HTTPException(
                    status_code=500,
                    detail=f"GitLab API returned {created.status_code} creating hook: {created.text}",
                )
            return "created"

        if all(existing.get(flag) for flag in required_flags):
            return "exists"

        updated = await self.http.put(f"{base}/{existing['id']}", headers=headers, json=desired)
        if updated.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {updated.status_code} updating hook: {updated.text}",
            )
        return "updated"

    async def ensure_group_labels(
        self,
        access_token: str,
        group_id: str,
        *,
        provider_url: str | None = None,
    ) -> None:
        """Create the jeanclode:* trigger labels on a group if they're missing.

        Runs once per connected group — GitLab inherits group labels down to
        every subgroup and project, so there's no need to touch each project.
        Best-effort: a failure here logs and returns rather than raising, so
        it never holds up the connect flow.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        headers = {"Authorization": f"Bearer {access_token}"}
        base = f"{instance_url}/api/v4/groups/{group_id}/labels"

        existing: set[str] = set()
        page = 1
        per_page = 100
        while True:
            resp = await self.http.get(
                base, headers=headers, params={"page": page, "per_page": per_page}
            )
            if resp.status_code != 200:
                logger.warning("Failed to list group labels for %s: %s", group_id, resp.status_code)
                return
            page_labels = resp.json()
            if not page_labels:
                break
            existing.update(label["name"] for label in page_labels)
            page += 1

        for name, (color, description) in _TRIGGER_LABELS.items():
            if name in existing:
                continue
            created = await self.http.post(
                base,
                headers=headers,
                json={"name": name, "color": color, "description": description},
            )
            if created.status_code not in (200, 201):
                logger.warning(
                    "Failed to create group label %s on %s: %s",
                    name,
                    group_id,
                    created.status_code,
                )

    async def delete_project_webhook(
        self,
        access_token: str,
        project_id: str,
        *,
        hook_url: str,
        provider_url: str | None = None,
    ) -> str:
        """Remove the Jeanclode webhook from a project, matched by URL.

        Returns ``"deleted"`` or ``"absent"``. Raises HTTPException on API
        failure; a project that no longer exists (404) counts as ``"absent"``.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        headers = {"Authorization": f"Bearer {access_token}"}
        base = f"{instance_url}/api/v4/projects/{project_id}/hooks"

        resp = await self.http.get(base, headers=headers, params={"per_page": 100})
        if resp.status_code == 404:
            return "absent"
        if resp.status_code != 200:
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {resp.status_code} listing hooks: {resp.text}",
            )
        existing = next((h for h in resp.json() if h.get("url") == hook_url), None)
        if existing is None:
            return "absent"

        deleted = await self.http.delete(f"{base}/{existing['id']}", headers=headers)
        if deleted.status_code not in (200, 204):
            raise HTTPException(
                status_code=500,
                detail=f"GitLab API returned {deleted.status_code} deleting hook: {deleted.text}",
            )
        return "deleted"

    async def list_notes(
        self,
        access_token: str,
        project_id: str,
        resource: str,
        iid: int,
        *,
        provider_url: str | None = None,
    ) -> list[dict]:
        """List notes on a merge request or issue.

        ``resource`` is ``"merge_requests"`` or ``"issues"``. Used to find an
        existing sticky status comment by marker before create-vs-update.
        """
        instance_url = provider_url or self.get_effective_instance_url()
        notes: list[dict] = []
        page = 1
        per_page = 100
        while True:
            response = await self.http.get(
                f"{instance_url}/api/v4/projects/{project_id}/{resource}/{iid}/notes",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"page": page, "per_page": per_page},
            )
            if response.status_code != 200:
                logger.warning(
                    "Failed to list notes for project %s %s/%s: %s",
                    project_id,
                    resource,
                    iid,
                    response.status_code,
                )
                break
            page_notes = response.json()
            if not page_notes:
                break
            notes.extend(page_notes)
            page += 1
        return notes

    async def create_note(
        self,
        access_token: str,
        project_id: str,
        resource: str,
        iid: int,
        body: str,
        *,
        provider_url: str | None = None,
    ) -> None:
        """Post a new note on a merge request or issue."""
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.post(
            f"{instance_url}/api/v4/projects/{project_id}/{resource}/{iid}/notes",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"body": body},
        )
        if response.status_code != 201:
            logger.warning(
                "Failed to create note on project %s %s/%s: %s",
                project_id,
                resource,
                iid,
                response.status_code,
            )

    async def update_note(
        self,
        access_token: str,
        project_id: str,
        resource: str,
        iid: int,
        note_id: int,
        body: str,
        *,
        provider_url: str | None = None,
    ) -> None:
        """Edit an existing note in place."""
        instance_url = provider_url or self.get_effective_instance_url()
        response = await self.http.put(
            f"{instance_url}/api/v4/projects/{project_id}/{resource}/{iid}/notes/{note_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"body": body},
        )
        if response.status_code != 200:
            logger.warning(
                "Failed to update note %s on project %s %s/%s: %s",
                note_id,
                project_id,
                resource,
                iid,
                response.status_code,
            )
