"""Database operations for Repository model."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, or_, true, update
from sqlalchemy.orm import Query, Session
from sqlalchemy.orm.util import AliasedClass

from api.database.organization import db_get_org_subtree_ids
from api.models.organizations import Organization, Provider
from api.models.repositories import MappingMethod, Repository, RepositoryMapping

_GIT_PROVIDERS = (Provider.GITHUB.value, Provider.GITLAB.value)


def repo_enabled_clause(entity: type[Repository] | AliasedClass = Repository):
    """``RepoSettings.enabled`` read straight from JSONB: absent means enabled.

    Takes the entity so callers can filter an aliased join — the Sentry
    dispatcher gates on the *mapped git repo*, not the Sentry project row.
    """
    return func.coalesce(entity.settings["enabled"].as_boolean(), true())


def db_get_repositories_by_org(
    db: Session,
    org_id: UUID,
) -> list[Repository]:
    """Get all repositories for an organization."""
    return db.query(Repository).filter(Repository.org_id == org_id).order_by(Repository.name).all()


def db_get_repositories_by_ids(db: Session, repo_ids: Sequence[UUID]) -> list[Repository]:
    """Repositories for the given ids, name-ordered. Missing ids are skipped."""
    if not repo_ids:
        return []
    return (
        db.query(Repository)
        .filter(Repository.id.in_(list(repo_ids)))
        .order_by(Repository.name)
        .all()
    )


def db_get_enabled_repositories_by_org(db: Session, org_id: UUID) -> list[Repository]:
    """An org's own repositories that still have triggers on.

    Direct members only — a GitLab subgroup's own projects, not its
    subgroups'. Used by the subgroup pack, which is an automatic net: a repo
    someone deliberately turned off shouldn't be dragged into runs by it.
    Explicit choices (a repo group, the always-include list) ignore the flag.
    """
    return (
        db.query(Repository)
        .filter(Repository.org_id == org_id, repo_enabled_clause())
        .order_by(Repository.name)
        .all()
    )


def db_count_org_repositories(
    db: Session,
    org_id: UUID,
    workspace_id: UUID,
) -> int:
    """Count an org's repositories, including those held by its subgroup orgs.

    A GitLab group's projects hang off an org per subgroup namespace rather
    than off the group itself, so a plain count on ``org_id`` under-reports
    the connection the tenant actually made.
    """
    return (
        db.query(func.count(Repository.id))
        .filter(Repository.org_id.in_(db_get_org_subtree_ids(db, [org_id], workspace_id)))
        .scalar()
        or 0
    )


def _org_repos_query(db: Session, org_id: UUID, search: str | None) -> Query[Repository]:
    """Base query for an org's repositories, optionally name-filtered.

    Covers the org's subgroups too — a GitLab group's projects hang off an org
    per subgroup namespace, and the group is the only one the UI can ask for.
    """
    org = db.query(Organization).filter(Organization.id == org_id).first()
    org_scope = (
        db_get_org_subtree_ids(db, [org_id], org.workspace_id)
        if org is not None and org.workspace_id is not None
        else [org_id]
    )
    query = db.query(Repository).filter(Repository.org_id.in_(org_scope))
    if search and search.strip():
        query = query.filter(Repository.name.ilike(f"%{search.strip()}%"))
    return query


def db_list_repositories_by_org(
    db: Session,
    org_id: UUID,
    *,
    page: int = 1,
    limit: int = 25,
    search: str | None = None,
) -> tuple[list[Repository], int]:
    """One page of an org's repositories (name order) plus the total count.

    ``search`` is a case-insensitive substring match on the repo name. Used by
    the dashboard repo list and the related-repo picker, both of which have to
    stay responsive for groups with hundreds of repositories.
    """
    query = _org_repos_query(db, org_id, search)
    total = query.count()
    rows = query.order_by(Repository.name).offset((page - 1) * limit).limit(limit).all()
    return rows, total


def db_count_enabled_repositories(db: Session, org_id: UUID, search: str | None = None) -> int:
    """How many of the org's repositories (matching ``search``) have triggers on.

    A repo with no stored ``enabled`` key counts as enabled — that is
    ``RepoSettings``' default, and repos are created without settings.
    """
    return (
        _org_repos_query(db, org_id, search)
        .filter(repo_enabled_clause())
        .with_entities(func.count(Repository.id))
        .scalar()
        or 0
    )


def db_set_org_repos_enabled(
    db: Session,
    org_id: UUID,
    *,
    enabled: bool,
    search: str | None = None,
) -> int:
    """Flip ``enabled`` on every repository of the org matching ``search``.

    Merged into the existing JSONB rather than replacing it, so any other
    per-repo setting added later survives a bulk toggle. Returns the row count.
    """
    repo_ids = [row.id for row in _org_repos_query(db, org_id, search).with_entities(Repository.id)]
    if not repo_ids:
        return 0
    result = db.execute(
        update(Repository)
        .where(Repository.id.in_(repo_ids))
        .values(settings=Repository.settings.op("||")(func.jsonb_build_object("enabled", enabled)))
    )
    db.commit()
    return result.rowcount or 0


def db_get_repository_by_external_id(
    db: Session,
    external_id: str,
) -> Repository | None:
    """Get repository by external ID."""
    return db.query(Repository).filter(Repository.external_id == external_id).first()


def db_get_repository_by_org_and_external_id(
    db: Session,
    org_id: UUID,
    external_id: str,
) -> Repository | None:
    """Get a repository by its external ID within an organization."""
    return (
        db.query(Repository)
        .filter(
            Repository.org_id == org_id,
            Repository.external_id == external_id,
        )
        .first()
    )


def db_create_repository(
    db: Session,
    org_id: UUID,
    external_id: str,
    name: str,
    provider: str,
    web_url: str | None = None,
    provider_url: str | None = None,
    auth_token_encrypted: str | None = None,
    avatar_url: str | None = None,
) -> Repository:
    """Create a new repository."""
    repository = Repository(
        org_id=org_id,
        external_id=external_id,
        name=name,
        provider=provider,
        web_url=web_url,
        provider_url=provider_url,
        auth_token_encrypted=auth_token_encrypted,
        avatar_url=avatar_url,
    )
    db.add(repository)
    db.commit()
    db.refresh(repository)
    return repository


def db_upsert_repository(
    db: Session,
    org_id: UUID,
    external_id: str,
    name: str,
    provider: str,
    web_url: str | None = None,
    provider_url: str | None = None,
    auth_token_encrypted: str | None = None,
    avatar_url: str | None = None,
) -> Repository:
    """Create or update a repository.

    If it already exists (by org + external_id), updates name.
    Otherwise creates a new record.
    """
    existing = db_get_repository_by_org_and_external_id(db, org_id, external_id)
    if existing:
        existing.name = name
        if web_url is not None:
            existing.web_url = web_url
        if avatar_url is not None:
            existing.avatar_url = avatar_url
        db.commit()
        db.refresh(existing)
        return existing

    return db_create_repository(
        db,
        org_id,
        external_id,
        name,
        provider,
        web_url=web_url,
        provider_url=provider_url,
        auth_token_encrypted=auth_token_encrypted,
        avatar_url=avatar_url,
    )


def db_delete_repository(
    db: Session,
    external_id: str,
) -> bool:
    """Delete repository by external ID."""
    repository = db_get_repository_by_external_id(db, external_id)
    if repository:
        db.delete(repository)
        db.commit()
        return True
    return False


def db_get_repository_by_id(
    db: Session,
    repo_id: UUID,
) -> Repository | None:
    """Get a repository by its database ID."""
    return db.query(Repository).filter(Repository.id == repo_id).first()


def db_update_repository_mapping(
    db: Session,
    repo_id: UUID,
    mapped_repo_id: UUID | None,
    mapping_method: str,
) -> Repository | None:
    """Upsert the at-most-one cross-source mapping for a repository.

    Cross-source mappings (Sentry/Linear project -> git repo) live as a
    single ``RepositoryMapping`` row keyed by ``repo_id`` — this function
    finds-and-updates that row rather than inserting a new one, since the
    schema doesn't enforce one-row-per-repo_id (repo-to-repo groups need
    many).

    Mirrors the old column-assignment semantics exactly, including for a
    clear (``mapped_repo_id=None``): both fields are always written, so a
    manual clear still records ``mapping_method="manual"`` on the row
    rather than deleting it — that's what lets
    ``db_get_unmapped_repositories`` tell "user explicitly cleared this,
    retry is fine" apart from "auto-resolved and the target repo was later
    removed, don't retry" (see its docstring).
    """
    repo = db.query(Repository).filter(Repository.id == repo_id).first()
    if not repo:
        return None

    if mapped_repo_id is not None:
        target = db.query(Repository).filter(Repository.id == mapped_repo_id).first()
        if not target or target.provider not in _GIT_PROVIDERS:
            raise ValueError("Mapping target must be a GitHub or GitLab repository")

    existing = db.query(RepositoryMapping).filter(RepositoryMapping.repo_id == repo_id).first()
    if existing:
        existing.mapped_repo_id = mapped_repo_id
        existing.mapping_method = mapping_method
    else:
        db.add(
            RepositoryMapping(
                repo_id=repo_id,
                mapped_repo_id=mapped_repo_id,
                mapping_method=mapping_method,
            )
        )
    db.commit()
    db.refresh(repo)
    return repo


def db_get_mapped_repositories(
    db: Session,
    org_id: UUID,
) -> list[Repository]:
    """Get repositories that have a cross-source mapping to a git repo."""
    return (
        db.query(Repository)
        .join(RepositoryMapping, RepositoryMapping.repo_id == Repository.id)
        .filter(
            Repository.org_id == org_id,
            RepositoryMapping.mapped_repo_id.isnot(None),
        )
        .all()
    )


def db_get_unmapped_repositories(
    db: Session,
    org_id: UUID,
) -> list[Repository]:
    """Get repositories eligible for auto-resolution.

    Returns projects with no mapping (no row, or a row with mapped_repo_id
    IS NULL), excluding those previously auto-resolved and then cleared —
    they keep their mapping_method (fuzzy/code_mapping) as a signal that
    auto-resolve already tried and the target repo was since removed (the
    FK's ON DELETE SET NULL nulls ``mapped_repo_id`` but leaves the row and
    its method in place).

    Manual clears are retried (could be a misclick) — those delete the row
    outright (see ``db_update_repository_mapping``), so they fall into the
    "no row" case alongside freshly synced projects.
    """
    return (
        db.query(Repository)
        .outerjoin(RepositoryMapping, RepositoryMapping.repo_id == Repository.id)
        .filter(
            Repository.org_id == org_id,
            RepositoryMapping.mapped_repo_id.is_(None),
            or_(
                RepositoryMapping.id.is_(None),
                RepositoryMapping.mapping_method.is_(None),
                RepositoryMapping.mapping_method == MappingMethod.MANUAL.value,
            ),
        )
        .all()
    )


def _canonical_pair(repo_a: UUID, repo_b: UUID) -> tuple[UUID, UUID]:
    """Sort a repo-group pair so (A, B) and (B, A) always store as one edge."""
    return (repo_a, repo_b) if str(repo_a) < str(repo_b) else (repo_b, repo_a)


def _same_workspace_gitlab(db: Session, a: Repository, b: Repository) -> bool:
    """True when both repos are GitLab repos whose orgs share one workspace.

    The only cross-organization repo grouping we allow: the dispatch layer
    already hands every GitLab token in a workspace to the container, so a
    clone of ``b`` from ``a``'s run authenticates against ``b``'s own group
    token via the proxy's per-namespace routing.
    """
    if a.provider != Provider.GITLAB.value or b.provider != Provider.GITLAB.value:
        return False
    org_a = db.query(Organization).filter(Organization.id == a.org_id).first()
    org_b = db.query(Organization).filter(Organization.id == b.org_id).first()
    return (
        org_a is not None
        and org_b is not None
        and org_a.workspace_id is not None
        and org_a.workspace_id == org_b.workspace_id
    )


def db_link_repos(db: Session, repo_a: UUID, repo_b: UUID) -> RepositoryMapping:
    """Link two git repos into a group.

    Canonicalizes pair order before insert so linking (A, B) and (B, A)
    always resolve to the same stored row — the relationship is undirected,
    storage isn't. Both repos must be GitHub/GitLab repos. They may live in
    different organizations only when both are GitLab repos in the same
    workspace: GitLab group tokens are per-group and the container dispatch
    already bundles every GitLab token in the workspace (path-prefix scoped),
    so a cross-group clone authorizes. GitHub is single-org only — one
    installation token can't span orgs. Always ``mapping_method=MANUAL``:
    unlike cross-source mappings, there's no code-mapping/fuzzy signal for
    "these two repos belong together", a tenant always declares it explicitly.
    """
    if repo_a == repo_b:
        raise ValueError("Cannot link a repository to itself")

    a = db.query(Repository).filter(Repository.id == repo_a).first()
    b = db.query(Repository).filter(Repository.id == repo_b).first()
    if not a or not b:
        raise ValueError("Both repositories must exist")
    if a.provider not in _GIT_PROVIDERS or b.provider not in _GIT_PROVIDERS:
        raise ValueError("Repo groups can only link GitHub/GitLab repositories")
    if a.org_id != b.org_id and not _same_workspace_gitlab(db, a, b):
        raise ValueError(
            "Cross-organization repo groups are only supported for GitLab "
            "repositories in the same workspace"
        )

    source_id, target_id = _canonical_pair(repo_a, repo_b)
    existing = (
        db.query(RepositoryMapping)
        .filter(
            RepositoryMapping.repo_id == source_id,
            RepositoryMapping.mapped_repo_id == target_id,
        )
        .first()
    )
    if existing:
        return existing

    mapping = RepositoryMapping(
        repo_id=source_id,
        mapped_repo_id=target_id,
        mapping_method=MappingMethod.MANUAL.value,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return mapping


def db_unlink_repos(db: Session, repo_a: UUID, repo_b: UUID) -> bool:
    """Remove a repo-group link, regardless of which side it was stored under."""
    source_id, target_id = _canonical_pair(repo_a, repo_b)
    existing = (
        db.query(RepositoryMapping)
        .filter(
            RepositoryMapping.repo_id == source_id,
            RepositoryMapping.mapped_repo_id == target_id,
        )
        .first()
    )
    if not existing:
        return False
    db.delete(existing)
    db.commit()
    return True


def db_get_related_repos(db: Session, repo_id: UUID) -> list[Repository]:
    """Get every git repo grouped with ``repo_id``, in either storage direction.

    Group edges are stored under a single canonical (repo_id, mapped_repo_id)
    row per pair, so membership has to be read from both columns. The
    provider filter on the reverse side excludes cross-source rows that
    happen to target this repo (e.g. a Sentry project mapped to it) — those
    aren't group members, just a different edge flavor sharing the table.
    """
    outgoing = (
        db.query(Repository)
        .join(RepositoryMapping, RepositoryMapping.mapped_repo_id == Repository.id)
        .filter(RepositoryMapping.repo_id == repo_id)
    )

    incoming = (
        db.query(Repository)
        .join(RepositoryMapping, RepositoryMapping.repo_id == Repository.id)
        .filter(
            RepositoryMapping.mapped_repo_id == repo_id,
            Repository.provider.in_(_GIT_PROVIDERS),
        )
    )

    return outgoing.union(incoming).all()


def db_get_related_repos_bulk(
    db: Session, repo_ids: Sequence[UUID]
) -> dict[UUID, list[Repository]]:
    """Group members for many repos at once, keyed by the repo asked for.

    The repo list on the git org page needs this for every row it renders.
    One query for the page instead of one per row, which is the difference
    between a page view costing one connection and costing twenty-five.
    """
    if not repo_ids:
        return {}

    ids = list(repo_ids)
    outgoing = (
        db.query(RepositoryMapping.repo_id.label("source_id"), Repository)
        .join(RepositoryMapping, RepositoryMapping.mapped_repo_id == Repository.id)
        .filter(RepositoryMapping.repo_id.in_(ids))
    )
    incoming = (
        db.query(RepositoryMapping.mapped_repo_id.label("source_id"), Repository)
        .join(RepositoryMapping, RepositoryMapping.repo_id == Repository.id)
        .filter(
            RepositoryMapping.mapped_repo_id.in_(ids),
            Repository.provider.in_(_GIT_PROVIDERS),
        )
    )

    grouped: dict[UUID, list[Repository]] = {rid: [] for rid in ids}
    seen: set[tuple[UUID, UUID]] = set()
    for source_id, repo in outgoing.union(incoming).all():
        if (source_id, repo.id) in seen:
            continue
        seen.add((source_id, repo.id))
        grouped[source_id].append(repo)
    return grouped
