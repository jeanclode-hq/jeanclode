"""Matching utilities for project-to-repo resolution."""

import re
from urllib.parse import urlparse
from uuid import UUID

from api.models.repositories import Repository


def normalize_url(url: str | None) -> str | None:
    """Normalize a URL for comparison by stripping scheme, trailing slashes, and .git suffix."""
    if not url:
        return None
    parsed = urlparse(url)
    normalized = f"{parsed.netloc}{parsed.path}".lower().rstrip("/")
    if normalized.endswith(".git"):
        normalized = normalized[:-4]
    return normalized


def normalize_name(name: str) -> str:
    """Normalize a repo/project name for name-based matching.

    Sentry project slugs are always bare (no namespace) and dash-separated.
    Repository.name isn't as consistent: GitLab repos may be stored either
    as the bare display name or as ``group/subgroup/repo-name`` depending on
    which sync path created them, and display names can contain spaces or
    mixed separators. Comparing on the last path segment with separators
    collapsed makes both sides line up regardless of which convention the
    repo's name was stored in.
    """
    short = name.rsplit("/", 1)[-1]
    return re.sub(r"[-_\s]+", "-", short.strip().lower())


def resolve_via_code_mappings(
    sentry_projects: list[Repository], repositories: list[Repository], code_mappings
) -> dict[UUID, UUID]:
    """Match projects to repos using Sentry code mappings.

    Code mappings contain repo_name (e.g., "org/repo") which we match
    against Repository.web_url or Repository.provider_url.

    Returns:
        dict mapping Repository.id -> Repository.id for matched projects
    """
    results: dict[UUID, UUID] = {}

    # Build lookup: normalized repo URL -> Repository
    repo_by_url: dict[str, Repository] = {}
    repo_by_name: dict[str, Repository] = {}
    for repo in repositories:
        for url in (repo.web_url, repo.provider_url):
            normalized = normalize_url(url)
            if normalized:
                repo_by_url[normalized] = repo
        repo_by_name[normalize_name(repo.name)] = repo

    project_by_slug = {p.name.lower(): p for p in sentry_projects}

    for cm in code_mappings:
        project = project_by_slug.get(cm.project_slug.lower())
        if not project or project.id in results:
            continue

        # Code mapping repo_name is typically "org/repo-name"
        repo_name_lower = cm.repo_name.lower()

        # Match repo_name suffix against normalized repo URLs
        matched_repo: Repository | None = None
        for normalized_url, repo in repo_by_url.items():
            if normalized_url and normalized_url.endswith(repo_name_lower):
                matched_repo = repo
                break

        # Fallback: match the last segment of repo_name (e.g. "org/repo" ->
        # "repo") against repo_by_name, which is itself keyed on the last
        # path segment — so this lines up whether Repository.name was stored
        # bare or namespaced.
        if not matched_repo:
            matched_repo = repo_by_name.get(normalize_name(cm.repo_name))

        if matched_repo:
            results[project.id] = matched_repo.id

    return results


def resolve_via_fuzzy_names(
    sentry_projects: list[Repository],
    repositories: list[Repository],
    already_matched: dict[UUID, UUID],
) -> dict[UUID, UUID]:
    """Match remaining projects to repos by name similarity.

    Compares the Sentry project slug against Repository.name, normalizing
    both to their bare, separator-collapsed last path segment — so
    "backend-api" matches "backend_api", "Backend API", and
    "some-group/backend-api" alike.

    Returns:
        dict mapping Repository.id -> Repository.id for matched projects
    """
    results: dict[UUID, UUID] = {}
    repo_by_name: dict[str, Repository] = {}
    for repo in repositories:
        repo_by_name.setdefault(normalize_name(repo.name), repo)

    for project in sentry_projects:
        if project.id in already_matched:
            continue

        matched_repo = repo_by_name.get(normalize_name(project.name))
        if matched_repo:
            results[project.id] = matched_repo.id

    return results
