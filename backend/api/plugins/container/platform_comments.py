"""Find-or-update comment/note upsert for GitHub and GitLab.

Scans existing comments/notes for a marker string and edits that one in
place instead of creating a new comment on every workflow run. This is
what keeps the sticky status comment (see ``status_comment.py``) to a
single comment per PR/issue, self-healing even if the comment is deleted
— there's no stored comment ID, just a live re-scan on every update.
"""

import logging

from api.plugins.github.plugin import GitHubPlugin
from api.plugins.gitlab.plugin import GitLabPlugin

logger = logging.getLogger(__name__)


async def upsert_github_comment(
    plugin: GitHubPlugin,
    *,
    installation_token: str,
    owner: str,
    repo: str,
    number: int,
    marker: str,
    body: str,
) -> None:
    """Create or update the marked bot comment on a GitHub PR/issue."""
    comments = await plugin.list_issue_comments(installation_token, owner, repo, number)
    existing = next((c for c in comments if marker in (c.get("body") or "")), None)
    if existing:
        await plugin.update_issue_comment(installation_token, owner, repo, existing["id"], body)
    else:
        await plugin.create_issue_comment(installation_token, owner, repo, number, body)


async def upsert_gitlab_note(
    plugin: GitLabPlugin,
    *,
    access_token: str,
    project_id: str,
    resource: str,
    iid: int,
    marker: str,
    body: str,
    provider_url: str | None = None,
) -> None:
    """Create or update the marked bot note on a GitLab MR/issue.

    ``resource`` is ``"merge_requests"`` or ``"issues"``.
    """
    notes = await plugin.list_notes(
        access_token, project_id, resource, iid, provider_url=provider_url
    )
    existing = next((n for n in notes if marker in (n.get("body") or "")), None)
    if existing:
        await plugin.update_note(
            access_token,
            project_id,
            resource,
            iid,
            existing["id"],
            body,
            provider_url=provider_url,
        )
    else:
        await plugin.create_note(
            access_token, project_id, resource, iid, body, provider_url=provider_url
        )
