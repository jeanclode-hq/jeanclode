"""Which repos get cloned alongside the one a run was triggered on.

Three sources, unioned into a set and then stripped of the primary:

1. **Repo groups** (``RepositoryMapping``) — symmetric and hand-made, so
   never capped.
2. **The org's always-include list** — one-directional: a run on any repo
   under the org pulls these in, a run on one of *them* pulls nothing back.
   Subtracting the primary last is what gives that asymmetry, and it's why
   this can't be stored as a mapping row.
3. **The subgroup pack** — the other repos sitting directly in the run's own
   subgroup. On by default, which is why it is also the only source with a
   ceiling: related repos are cloned sequentially before triage runs
   (``cli/src/runner/preflight.py``) and priced into the ``/tmp`` emptyDir
   (``sizing.py``), so a 250-project subgroup would spend the run's whole
   timeout cloning. Above ``MAX_PACKED_SUBGROUP_REPOS`` the pack is skipped
   whole rather than truncated — an arbitrary 30 of 250 is worse than none,
   since it silently omits the repo the fix needed.

The connected group is the only org the UI offers, so settings are read
through the org chain: a subgroup's own row is empty and reading it raw
would hand every subgroup the defaults instead of what the tenant set.
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from api.database.organization import db_get_org_by_id, db_resolve_org_settings
from api.database.repository import (
    db_get_enabled_repositories_by_org,
    db_get_related_repos,
    db_get_repositories_by_ids,
)
from api.models.repositories import Repository
from api.models.settings import RelatedRepoSettings

logger = logging.getLogger(__name__)

# Ceiling on the automatic subgroup pack only — see the module docstring.
MAX_PACKED_SUBGROUP_REPOS = 30


def resolve_related_repos(db: Session, repo: Repository | None) -> list[Repository]:
    """Every repo that should be cloned alongside ``repo``, name-ordered."""
    if repo is None:
        return []

    related: dict[UUID, Repository] = {r.id: r for r in db_get_related_repos(db, repo.id)}

    org = db_get_org_by_id(db, repo.org_id)
    if org is not None:
        settings = RelatedRepoSettings.model_validate(
            db_resolve_org_settings(db, org).get("related_repos") or {}
        )
        for pinned in db_get_repositories_by_ids(db, settings.always_include):
            related.setdefault(pinned.id, pinned)

        # A repo hanging off the connected group itself has no subgroup to
        # pack — packing there would mean the whole connection.
        if (
            settings.pack_subgroup
            and org.parent_org_id
            and org.id not in settings.excluded_subgroups
        ):
            siblings = db_get_enabled_repositories_by_org(db, org.id)
            if len(siblings) > MAX_PACKED_SUBGROUP_REPOS:
                logger.warning(
                    "subgroup pack skipped for %s: %s holds %d repos (limit %d) — "
                    "group the ones that belong together, or exclude the subgroup",
                    repo.name,
                    org.name,
                    len(siblings),
                    MAX_PACKED_SUBGROUP_REPOS,
                )
            else:
                for sibling in siblings:
                    related.setdefault(sibling.id, sibling)

    # Last, so "always-include C" adds nothing to a run on C itself.
    related.pop(repo.id, None)
    return sorted(related.values(), key=lambda r: r.name)
