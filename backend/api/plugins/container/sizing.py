"""Dynamic ``/tmp`` emptyDir sizing for sandboxed CLI containers.

Each dispatch clones a group of repos (the primary repo plus whatever
related repos are grouped with it via ``RepositoryMapping``) into a single
``/tmp`` emptyDir volume, so the size is computed per-dispatch rather than
using one static value for every execution.

What a clone writes is the **working tree plus the shallow pack**, and the
providers' headline size field measures only the packed object store. For
compressible content those differ by more than an order of magnitude: one
40Mi repo of load-test JSON checks out at 1.3Gi, and sizing off the packed
number evicted the pod mid-run. So the working tree is priced by summing
every blob at HEAD, and the packed size is added on top to cover the
``.git`` the clone also writes.

Every repo in the group is priced concurrently, and a per-repo margin
covers what blob sizes don't: filesystem block rounding, dependency
installs, build output, the CLI's own scratch.
"""

from __future__ import annotations

import asyncio
import logging
import math

from api.context import get_current_app
from api.models.repositories import Repository

logger = logging.getLogger(__name__)

# Per-repo headroom on top of the measured checkout. Scales with the group
# because each repo brings its own block rounding, installs and build
# output — one flat figure spread across a group is what let a five-repo
# batch through on a budget sized for one.
MARGIN_PER_REPO_MI = 250

# Smallest /tmp ever handed out. With real measurements this is only the
# backstop for a group whose sizes could not be resolved at all.
FLOOR_MI = 3072


async def resolve_tmp_size_limit(
    primary: Repository | None, related: list[Repository] | None = None
) -> str:
    """Price ``primary`` + every repo in ``related``, return a K8s quantity.

    Best-effort per repo: one whose size can't be fetched contributes only
    its margin rather than failing the dispatch — sizing is an optimization,
    not a precondition for running. Always returns a value (the floor, at
    minimum), never raises.
    """
    repos = list({r.id: r for r in [primary, *(related or [])] if r is not None}.values())
    if not repos:
        return f"{FLOOR_MI}Mi"

    sizes = await asyncio.gather(*(_fetch_repo_disk_kb(r) for r in repos), return_exceptions=True)

    total_kb = 0
    for repo, size_kb in zip(repos, sizes, strict=True):
        if isinstance(size_kb, BaseException):
            logger.warning("size lookup raised for repo %s: %s", repo.id, size_kb)
            continue
        if size_kb is not None:
            total_kb += size_kb

    size_mi = max(total_kb / 1024 + MARGIN_PER_REPO_MI * len(repos), FLOOR_MI)
    return f"{math.ceil(size_mi)}Mi"


async def _fetch_repo_disk_kb(repo: Repository) -> int | None:
    """What one repo's shallow clone writes: working tree + packed ``.git``."""
    from api.database.organization import db_get_org_by_id

    app = get_current_app()
    db_plugin = app.database
    if not db_plugin:
        return None

    # Re-read the row rather than trusting the caller's instance: these
    # arrive detached from the launcher's closure, and a token or URL that
    # was never loaded would raise on attribute access.
    with db_plugin.session() as db:
        row = db.get(Repository, repo.id)
        if row is None:
            logger.warning("size lookup skipped: repo %s missing", repo.id)
            return None
        org = db_get_org_by_id(db, row.org_id)
        if not org:
            logger.warning("size lookup skipped: org %s missing for repo %s", row.org_id, repo.id)
            return None
        provider = org.provider
        installation_id = org.installation_id
        external_id = row.external_id
        full_path = row.name
        # Same precedence the clone credentials use: a repo connected with
        # its own token is not covered by its org's, and pricing it off the
        # org alone silently values it at zero.
        auth_token_encrypted = row.auth_token_encrypted or org.auth_token_encrypted
        base_url = row.provider_url or org.base_url

    try:
        if provider == "github":
            if not installation_id:
                logger.warning("size lookup skipped: no installation for repo %s", repo.id)
                return None
            return await _fetch_github_disk_kb(installation_id, external_id)
        if provider == "gitlab":
            if not auth_token_encrypted:
                logger.warning("size lookup skipped: no token for repo %s", repo.id)
                return None
            return await _fetch_gitlab_disk_kb(
                auth_token_encrypted, base_url, external_id, full_path
            )
    except Exception:
        logger.exception("size lookup failed for repo %s (provider=%s)", repo.id, provider)
        return None

    logger.warning("size lookup skipped: unsupported provider=%s for repo %s", provider, repo.id)
    return None


async def _fetch_github_disk_kb(installation_id: str, external_id: str) -> int | None:
    app = get_current_app()
    if not app.github:
        return None
    token = await app.github.get_installation_access_token(installation_id)
    data = await app.github.fetch_repository_by_id(token, external_id)
    if not data:
        return None
    packed_kb = int(data.get("size") or 0)
    tree_bytes = await app.github.fetch_repo_tree_bytes(
        token, external_id, data.get("default_branch") or "HEAD"
    )
    if tree_bytes is None:
        logger.warning("checkout size unavailable for repo %s, using packed size", external_id)
        return packed_kb
    return packed_kb + math.ceil(tree_bytes / 1024)


async def _fetch_gitlab_disk_kb(
    auth_token_encrypted: str, base_url: str | None, external_id: str, full_path: str
) -> int | None:
    app = get_current_app()
    db_plugin = app.database
    if not app.gitlab or not db_plugin:
        return None
    token = db_plugin.decrypt(auth_token_encrypted)

    project, tree_bytes = await asyncio.gather(
        app.gitlab.fetch_project(token, external_id, provider_url=base_url, statistics=True),
        app.gitlab.fetch_project_tree_bytes(token, external_id, full_path, base_url),
    )
    stats = (project or {}).get("statistics") or {}
    packed_bytes = stats.get("repository_size")
    if packed_bytes is None:
        # GitLab omits `statistics` entirely for a token without permission
        # to read them, so this is a silent zero, not an error.
        logger.warning("size lookup: no statistics returned for project %s", external_id)
        packed_bytes = 0

    if tree_bytes is None:
        logger.warning("checkout size unavailable for project %s, using packed size", external_id)
        if not packed_bytes:
            return None
        return math.ceil(packed_bytes / 1024)

    return math.ceil((int(packed_bytes) + tree_bytes) / 1024)
