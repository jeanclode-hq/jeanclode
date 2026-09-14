"""Database operations for Organization model."""

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.util import AliasedClass

from api.models.identities import ProviderIdentity
from api.models.organizations import MemberRole, Organization, OrgMembership
from api.models.repositories import Repository
from api.models.settings import BATCH_WINDOW_MINUTES, BatchWindow, merge_settings


def batch_window_minutes_clause(entity: type[Organization] | AliasedClass = Organization):
    """``SentryOrgSettings.batch_window`` read from JSONB, as a minutes SQL expression.

    Absent (or unrecognized) settings fall back to the 5-minute default, kept
    in sync with the Pydantic default via ``BATCH_WINDOW_MINUTES``. ``entity``
    may be an ``aliased(Organization)`` — the per-git-org dispatch query reads
    the window off the *Sentry* org alias while claiming the *git* org row.
    """
    return case(
        *(
            (entity.settings["batch_window"].as_string() == window.value, minutes)
            for window, minutes in BATCH_WINDOW_MINUTES.items()
        ),
        else_=BATCH_WINDOW_MINUTES[BatchWindow.MINUTES_5],
    )


def db_get_org_by_id(
    db: Session,
    org_id: UUID,
) -> Organization | None:
    """Get organization by ID."""
    return db.query(Organization).filter(Organization.id == org_id).first()


def db_get_org_by_installation_id(
    db: Session,
    installation_id: str,
) -> Organization | None:
    """Get organization by installation ID."""
    return db.query(Organization).filter(Organization.installation_id == installation_id).first()


_UNSET = object()


def db_get_org_by_external_id(
    db: Session,
    external_org_id: str,
    provider: str | None = None,
    workspace_id: UUID | None = None,
    base_url: str | None = _UNSET,  # type: ignore[assignment]
) -> Organization | None:
    """Get organization by external org ID, optionally scoped by provider/workspace/base_url.

    ``external_org_id`` is only unique per GitLab *instance* — numeric group/user
    namespace IDs are assigned independently by each instance, so gitlab.com and
    a self-hosted instance can easily share the same ID (this mirrors the
    ``uq_org_provider_external`` constraint on ``(provider, external_org_id,
    base_url)``). Callers resolving a GitLab org therefore MUST pass ``base_url``
    explicitly, even when it's ``None`` (gitlab.com) — that still needs to filter
    to orgs with no base_url, not match every instance's org. Leaving the
    parameter unset (the default) skips base_url filtering entirely, for
    providers like GitHub/Sentry where org IDs are already globally unique.
    """
    query = db.query(Organization).filter(Organization.external_org_id == external_org_id)
    if provider:
        query = query.filter(Organization.provider == provider)
    if workspace_id:
        query = query.filter(Organization.workspace_id == workspace_id)
    if base_url is not _UNSET:
        query = query.filter(Organization.base_url == base_url)
    return query.first()


def db_get_orgs_by_workspace(
    db: Session,
    workspace_id: UUID,
    provider: str | None = None,
    require_token: bool = True,
) -> list[Organization]:
    """Get organizations in a workspace, optionally filtered by provider.

    Placeholder ancestor orgs (auth_token_encrypted IS NULL) exist only for
    hierarchy metadata; by default they're excluded since most callers need
    an org they can actually authenticate with. Pass require_token=False for
    callers that must see every org in the hierarchy, e.g. to fall back to
    repo-level tokens owned by an org that has none of its own.
    """
    query = db.query(Organization).filter(Organization.workspace_id == workspace_id)
    if require_token:
        query = query.filter(Organization.auth_token_encrypted.isnot(None))
    if provider:
        query = query.filter(Organization.provider == provider)
    return query.all()


def db_list_gitlab_orgs(db: Session) -> list[Organization]:
    """Return all GitLab organizations across all workspaces."""
    return db.query(Organization).filter(Organization.provider == "gitlab").all()


def db_resolve_org_token(db: Session, org: Organization) -> str | None:
    """Return the encrypted token covering ``org``, walking ancestors upward.

    A GitLab subgroup org is usually token-less: the group access token sits on
    the ancestor that was actually connected, and GitLab's inherited
    permissions make it valid for everything below. Anything acting on behalf
    of an org — a manual re-sync, a system hook — has to resolve it the same
    way ``GitLabPlugin.resolve_repo_token`` does for a repo.
    """
    seen: set[UUID] = set()
    current: Organization | None = org
    while current and current.id not in seen:
        if current.auth_token_encrypted:
            return current.auth_token_encrypted
        seen.add(current.id)
        if not current.parent_org_id:
            break
        current = db_get_org_by_id(db, current.parent_org_id)
    return None


def _children_by_parent_global(db: Session, org_id: UUID) -> dict[UUID, list[Organization]]:
    """Index the orgs below ``org_id`` by parent, without scoping to a workspace.

    Deletion has to reach every child row whatever workspace it claims to be
    in — a subgroup that ended up mis-scoped is still garbage once its group
    is gone, and leaving it behind is what strands it as a phantom root.
    """
    children_by_parent: dict[UUID, list[Organization]] = {}
    frontier = [org_id]
    seen = {org_id}
    while frontier:
        children = db.query(Organization).filter(Organization.parent_org_id.in_(frontier)).all()
        frontier = []
        for child in children:
            if child.id in seen:
                continue
            seen.add(child.id)
            children_by_parent.setdefault(child.parent_org_id, []).append(child)  # type: ignore[arg-type]
            frontier.append(child.id)
    return children_by_parent


def _children_by_parent(db: Session, workspace_id: UUID) -> dict[UUID, list[Organization]]:
    """Index a workspace's orgs by ``parent_org_id`` for hierarchy walks."""
    all_orgs = db.query(Organization).filter(Organization.workspace_id == workspace_id).all()
    children_by_parent: dict[UUID, list[Organization]] = {}
    for org in all_orgs:
        if org.parent_org_id:
            children_by_parent.setdefault(org.parent_org_id, []).append(org)
    return children_by_parent


def _walk_descendants(
    children_by_parent: dict[UUID, list[Organization]],
    org_ids: Iterable[UUID],
) -> list[Organization]:
    """Every org strictly below any of ``org_ids``, each returned once."""
    descendants: list[Organization] = []
    seen: set[UUID] = set(org_ids)
    queue = [child for org_id in seen for child in children_by_parent.get(org_id, [])]
    while queue:
        child = queue.pop()
        if child.id in seen:
            continue
        seen.add(child.id)
        descendants.append(child)
        queue.extend(children_by_parent.get(child.id, []))
    return descendants


def db_get_descendant_orgs(
    db: Session,
    org_id: UUID,
    workspace_id: UUID,
) -> list[Organization]:
    """All orgs in the workspace strictly below ``org_id`` in the parent_org_id chain.

    Used when a group/subgroup token is added for an org that already has
    connected descendants (e.g. a subgroup token was added first, then the
    parent group's token) — the caller needs these to reconcile the
    descendants' now-redundant tokens onto the new ancestor token, the same
    way a project's own token is cleared when its org gets one (see
    ``_handle_group_token``'s ``existing_org`` branch).

    Walks ``parent_org_id`` edges rather than filtering by ``root_org_id``
    because ``org_id`` itself need not be a root (S4: subgroup tokens can be
    added directly without ever connecting the top-level group).
    """
    return _walk_descendants(_children_by_parent(db, workspace_id), [org_id])


def org_effective_root_id(entity: type[Organization] | AliasedClass = Organization):
    """SQL expression for ``entity``'s ultimate root org id.

    ``root_org_id`` is only set on non-root orgs (S1/S4 placeholder rows
    created by ``db_upsert_org_with_ancestors``); the root itself has it
    NULL, so falling back to its own id here always yields the top of the
    chain a single git token/bot identity actually covers.
    """
    return func.coalesce(entity.root_org_id, entity.id)


def db_get_org_subtree_ids(
    db: Session,
    org_ids: Iterable[UUID],
    workspace_id: UUID,
) -> list[UUID]:
    """``org_ids`` plus every org below them in the workspace's parent_org_id chain.

    The UI only offers the connected group as a source (see
    ``db_get_source_orgs``), so anything scoped to that group has to reach the
    subgroup orgs its projects actually hang off — otherwise everything under
    a subgroup drops out of the very views the group is meant to cover.
    """
    roots = list(dict.fromkeys(org_ids))
    if not roots:
        return []
    children_by_parent = _children_by_parent(db, workspace_id)
    return [*roots, *(org.id for org in _walk_descendants(children_by_parent, roots))]


def db_get_subgroup_repo_counts(db: Session, org: Organization) -> list[tuple[Organization, int]]:
    """Every org below ``org``, with the repos hanging *directly* off each.

    Direct members only, because that's what the subgroup pack clones — a
    parent subgroup's count must not include its children's projects, or the
    number next to the exclusion picker would describe a set nothing loads.
    """
    if org.workspace_id is None:
        return []
    ids = [oid for oid in db_get_org_subtree_ids(db, [org.id], org.workspace_id) if oid != org.id]
    if not ids:
        return []
    rows = (
        db.query(Organization, func.count(Repository.id))
        .outerjoin(Repository, Repository.org_id == Organization.id)
        .filter(Organization.id.in_(ids))
        .group_by(Organization.id)
        .order_by(Organization.name)
        .all()
    )
    return [(subgroup, count or 0) for subgroup, count in rows]


def db_get_source_orgs(
    db: Session,
    workspace_id: UUID,
) -> list[Organization]:
    """Orgs in a workspace that stand for a connection of their own.

    A connection is the org a credential was actually attached to. GitLab
    tokens inherit downward — a group token covers every subgroup beneath it —
    so an org sitting under a token-holding ancestor isn't its own connection,
    it's part of that ancestor's, and listing it would offer the tenant a
    source they never connected. The hierarchy decides this, not the token
    columns: the group repo sync copies the group token onto every project row
    it creates, subgroup projects included, so a subgroup's repositories look
    credentialed either way.

    Hierarchy-only placeholders are excluded on top of that — orgs with
    neither a token of their own nor any repository. An org with repositories
    but no org-level token stays: a GitLab namespace connected via a project
    access token keeps that token on the Repository row.
    """
    orgs = db_get_orgs_by_workspace(db, workspace_id, require_token=False)
    by_id = {org.id: org for org in orgs}
    # One query rather than a lazy load per org — a connected group can carry
    # dozens of subgroup orgs and hundreds of repositories between them.
    orgs_with_repos = {
        row[0]
        for row in db.query(Repository.org_id).filter(Repository.org_id.in_(by_id)).distinct().all()
    }

    def covered_by_ancestor(org: Organization) -> bool:
        seen = {org.id}
        parent_id = org.parent_org_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent is None:
                return False
            if parent.auth_token_encrypted:
                return True
            parent_id = parent.parent_org_id
        return False

    return [
        org
        for org in orgs
        if (org.auth_token_encrypted or org.id in orgs_with_repos) and not covered_by_ancestor(org)
    ]


def db_get_connection_org_id(db: Session, org_id: UUID) -> UUID:
    """The org that carries configuration for ``org_id``.

    Configuration — MCP servers, connector credentials, installed plugins —
    is attached to the org a tenant connected, because that is the only org
    the UI ever offers them. A run on a project inside a GitLab subgroup is
    keyed to that subgroup's org, which holds none of it, so anything read
    straight off the repository's org comes back empty. Walk up to the
    nearest token-holding ancestor; an org with no such ancestor is itself
    the connection (a namespace connected by a project access token keeps
    its token on the repository row).
    """
    seen: set[UUID] = set()
    current_id: UUID | None = org_id
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        row = (
            db.query(Organization.auth_token_encrypted, Organization.parent_org_id)
            .filter(Organization.id == current_id)
            .first()
        )
        if row is None:
            break
        if row[0] is not None:
            return current_id
        current_id = row[1]
    return org_id


def db_resolve_org_settings(db: Session, org: Organization) -> dict[str, Any]:
    """An org's settings with its ancestors' layered underneath.

    Only the connected group is offered in the UI, so that is the only org a
    tenant can configure — but a subgroup's projects hang off a subgroup org
    whose own settings are empty. Reading those raw hands every subgroup
    repository the built-in defaults instead of the triggers the tenant
    actually set. Ancestors are merged root-first, so a nearer org still
    overrides a key it sets for itself.
    """
    chain: list[dict[str, Any]] = []
    seen: set[UUID] = set()
    current: Organization | None = org
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current.settings or {})
        current = db_get_org_by_id(db, current.parent_org_id) if current.parent_org_id else None

    resolved: dict[str, Any] = {}
    for settings in reversed(chain):
        resolved = merge_settings(resolved, settings)
    return resolved


def db_create_org(
    db: Session,
    workspace_id: UUID | None,
    name: str,
    external_org_id: str,
    provider: str,
    installation_id: str | None = None,
    base_url: str | None = None,
    avatar_url: str | None = None,
    auth_token_encrypted: str | None = None,
    client_secret_encrypted: str | None = None,
    parent_org_id: UUID | None = None,
    root_org_id: UUID | None = None,
    settings: dict | None = None,
) -> Organization:
    """Create a new organization."""
    org = Organization(
        workspace_id=workspace_id,
        name=name,
        external_org_id=external_org_id,
        provider=provider,
        installation_id=installation_id,
        base_url=base_url,
        avatar_url=avatar_url,
        auth_token_encrypted=auth_token_encrypted,
        client_secret_encrypted=client_secret_encrypted,
        parent_org_id=parent_org_id,
        root_org_id=root_org_id,
        settings=settings if settings is not None else {},
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def db_upsert_org_with_ancestors(
    db: Session,
    workspace_id: UUID,
    provider: str,
    ancestors: list[dict],
    base_url: str | None = None,
) -> tuple[UUID | None, UUID | None]:
    """Upsert placeholder orgs for every ancestor in the chain.

    ``ancestors`` is ordered from immediate parent to root, as returned by
    ``GET /api/v4/groups/{id}/ancestors``.

    Placeholder orgs have no ``auth_token_encrypted``; they exist only to
    represent the hierarchy. An existing org is never overwritten — its ID is
    reused as-is.

    Returns:
        (parent_org_id, root_org_id) for the caller to set on the new org,
        or (None, None) when ``ancestors`` is empty.
    """
    if not ancestors:
        return None, None

    # Process root-first so each ancestor can reference its own parent
    reversed_ancestors = list(reversed(ancestors))
    ancestor_ids: list[UUID] = []

    for i, ancestor in enumerate(reversed_ancestors):
        external_id = str(ancestor["id"])
        existing = db_get_org_by_external_id(db, external_id, provider=provider, base_url=base_url)

        if existing:
            ancestor_ids.append(existing.id)
            continue

        anc_parent_id = ancestor_ids[i - 1] if i > 0 else None
        anc_root_id = ancestor_ids[0] if i > 0 else None

        placeholder = db_create_org(
            db=db,
            workspace_id=workspace_id,
            name=ancestor.get("name", ""),
            external_org_id=external_id,
            provider=provider,
            base_url=base_url,
            avatar_url=ancestor.get("avatar_url"),
            parent_org_id=anc_parent_id,
            root_org_id=anc_root_id,
        )
        ancestor_ids.append(placeholder.id)

    # reversed_ancestors[-1] is the immediate parent (first in original ancestors list)
    parent_org_id = ancestor_ids[-1]
    root_org_id = ancestor_ids[0]
    return parent_org_id, root_org_id


def db_update_org(
    db: Session,
    org_id: UUID,
    **kwargs: Any,
) -> Organization | None:
    """Update organization fields."""
    org = db_get_org_by_id(db, org_id)
    if not org:
        return None

    for key, value in kwargs.items():
        if hasattr(org, key):
            setattr(org, key, value)

    db.commit()
    db.refresh(org)
    return org


def _delete_org_tree(db: Session, org: Organization) -> None:
    """Delete an org together with everything below it in the hierarchy.

    A GitLab subgroup org exists only to hold the projects of the group that
    was connected; on its own it has no token, no membership and no way back
    into the UI. Leaving it behind when its group is deleted strands it as a
    root of its own — which is exactly how deleting one connected group turns
    into dozens of phantom ones.

    Placeholder ancestors above the deleted org are pruned too, once nothing
    is left under them: they were only ever created to carry the hierarchy.
    """
    parent_id = org.parent_org_id

    # Bulk-delete the descendants in one statement, then the org itself
    # through the ORM so its own repositories and memberships cascade the
    # way every other org delete does. Descendant repositories go with them
    # on the repositories.org_id FK, which has always been ON DELETE CASCADE.
    descendant_ids = [
        descendant.id
        for descendant in _walk_descendants(_children_by_parent_global(db, org.id), [org.id])
    ]
    if descendant_ids:
        db.query(Organization).filter(Organization.id.in_(descendant_ids)).delete(
            synchronize_session=False
        )
    db.delete(org)
    db.flush()

    while parent_id is not None:
        parent = db_get_org_by_id(db, parent_id)
        if parent is None or parent.auth_token_encrypted:
            break
        has_children = (
            db.query(Organization.id).filter(Organization.parent_org_id == parent.id).first()
        )
        has_repos = db.query(Repository.id).filter(Repository.org_id == parent.id).first()
        # A membership means someone claimed this org, so it is not the
        # bookkeeping row it looks like — leave it alone.
        has_members = db.query(OrgMembership.id).filter(OrgMembership.org_id == parent.id).first()
        if has_children or has_repos or has_members:
            break
        grandparent_id = parent.parent_org_id
        db.delete(parent)
        db.flush()
        parent_id = grandparent_id

    db.commit()


def db_delete_org(
    db: Session,
    installation_id: str,
) -> bool:
    """Delete organization by installation ID, and everything below it."""
    org = db_get_org_by_installation_id(db, installation_id)
    if org:
        _delete_org_tree(db, org)
        return True
    return False


def db_delete_org_by_id(
    db: Session,
    org_id: UUID,
) -> bool:
    """Delete an org by ID, with its subgroups (cascades to repos, memberships, etc.)."""
    org = db_get_org_by_id(db, org_id)
    if org:
        _delete_org_tree(db, org)
        return True
    return False


def db_delete_org_membership(
    db: Session,
    org_id: UUID,
    provider_identity_id: UUID,
) -> bool:
    """Delete an OrgMembership for a given org and provider identity."""
    membership = (
        db.query(OrgMembership)
        .filter(
            OrgMembership.org_id == org_id,
            OrgMembership.provider_identity_id == provider_identity_id,
        )
        .first()
    )
    if membership:
        db.delete(membership)
        db.commit()
        return True
    return False


def db_ensure_org_membership(
    db: Session,
    org_id: UUID,
    provider_identity_id: UUID,
    role: str = MemberRole.OWNER.value,
) -> OrgMembership:
    """Create an OrgMembership if one doesn't already exist."""
    existing = (
        db.query(OrgMembership)
        .filter(
            OrgMembership.org_id == org_id,
            OrgMembership.provider_identity_id == provider_identity_id,
        )
        .first()
    )
    if existing:
        return existing

    membership = OrgMembership(
        org_id=org_id,
        provider_identity_id=provider_identity_id,
        role=role,
    )
    db.add(membership)
    db.commit()
    db.refresh(membership)
    return membership


def db_claim_dispatch_window(
    db: Session,
    org_id: UUID,
    window_minutes: int,
) -> bool:
    """Atomically claim the dispatch window for an Organization.

    ``window_minutes`` is passed in rather than read from the row's own
    settings: Sentry dispatch partitions by *root git* org
    (``org_effective_root_id``), and the window it honours is the *Sentry*
    org's ``batch_window`` setting — a different row (ADR-006 / the per-git-org
    batching design). ``org_id`` here is expected to already be that root, so
    ``Organization.last_dispatched_at`` on it is now shared by every Sentry
    org whose issues map into any subgroup under that root — a deliberate
    trade for the merge gate/concurrency perimeter to hold across the whole
    connected group instead of leaking between its subgroups.
    """
    result = db.execute(
        update(Organization)
        .where(
            Organization.id == org_id,
            (Organization.last_dispatched_at.is_(None))
            | (
                Organization.last_dispatched_at
                < func.now() - func.make_interval(0, 0, 0, 0, 0, window_minutes, 0)
            ),
        )
        .values(last_dispatched_at=func.now())
    )
    db.commit()
    return result.rowcount > 0


def db_get_org_chain_ids(db: Session, org: Organization) -> list[UUID]:
    """``org``'s id followed by its ancestors', nearest first.

    The same chain :func:`db_resolve_org_settings` layers settings over.
    Membership rows are written by ``sync_org_members`` against whichever
    org was actually connected, so a notify list configured on a group has
    to be resolved against that group's memberships even when the dispatch
    is for a subgroup org that has none of its own.
    """
    chain: list[UUID] = []
    seen: set[UUID] = set()
    current: Organization | None = org
    while current is not None and current.id not in seen:
        seen.add(current.id)
        chain.append(current.id)
        current = db_get_org_by_id(db, current.parent_org_id) if current.parent_org_id else None
    return chain


def db_get_org_members(
    db: Session, org: Organization
) -> list[tuple[OrgMembership, ProviderIdentity]]:
    """Members of ``org`` and its ancestors, deduplicated by identity.

    Identities with ``user_id=NULL`` are included on purpose: those are
    provider members pre-populated by ``sync_org_members`` who never
    logged into Jeanclode, and they are exactly the people a tenant wants
    to notify. Nearest org in the chain wins on a duplicate identity so
    the role shown is the most specific one.
    """
    chain = db_get_org_chain_ids(db, org)
    if not chain:
        return []
    rows = (
        db.query(OrgMembership, ProviderIdentity)
        .join(ProviderIdentity, OrgMembership.provider_identity_id == ProviderIdentity.id)
        .filter(OrgMembership.org_id.in_(chain))
        .all()
    )
    rank = {org_id: i for i, org_id in enumerate(chain)}
    by_identity: dict[UUID, tuple[OrgMembership, ProviderIdentity]] = {}
    for membership, identity in rows:
        current = by_identity.get(identity.id)
        if current is None or rank[membership.org_id] < rank[current[0].org_id]:
            by_identity[identity.id] = (membership, identity)
    return sorted(by_identity.values(), key=lambda r: (r[1].username or "").lower())


def db_resolve_notify_handles(
    db: Session, org: Organization, identity_ids: Iterable[UUID]
) -> list[str]:
    """Provider handles for ``identity_ids``, scoped to ``org``'s members.

    Anything that isn't a current member of the org chain is dropped rather
    than trusted: a stale id left in settings after someone was removed
    from the org must not keep pinging them, and an id belonging to another
    provider must never resolve to a handle that gets @-mentioned into an
    unrelated instance's MR.
    """
    wanted = list(dict.fromkeys(identity_ids))
    if not wanted:
        return []
    allowed = {
        identity.id: identity.username
        for _membership, identity in db_get_org_members(db, org)
        if identity.username and identity.provider == org.provider
    }
    return [allowed[i] for i in wanted if i in allowed]
