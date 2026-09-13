"""Tests for /tmp emptyDir sizing.

A five-repo batch was evicted mid-run because sizing priced the packed
object store instead of the checkout: 80Mi of packed objects, 1.4Gi on
disk. These pin the pieces of the replacement — blob sums, the packed
`.git` on top, a margin that scales per repo, and the silent-zero paths
that hid the undercount.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from api.plugins.container.sizing import (
    FLOOR_MI,
    MARGIN_PER_REPO_MI,
    resolve_tmp_size_limit,
)
from api.plugins.sentry.launch import _resolve_mapped_repos

_GET_APP = "api.plugins.container.sizing.get_current_app"
_ORG_BY_ID = "api.database.organization.db_get_org_by_id"

MIB = 1024 * 1024


def _make_repo(*, auth_token_encrypted: str | None = None, external_id: str = "1") -> MagicMock:
    repo = MagicMock()
    repo.id = uuid4()
    repo.org_id = uuid4()
    repo.external_id = external_id
    repo.name = f"group/repo-{external_id}"
    repo.auth_token_encrypted = auth_token_encrypted
    repo.provider_url = None
    return repo


def _make_org(
    *, provider: str = "gitlab", auth_token_encrypted: str | None = "enc-org"
) -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    org.provider = provider
    org.auth_token_encrypted = auth_token_encrypted
    org.installation_id = None
    org.base_url = "https://gitlab.example.com"
    return org


def _make_app(repos: list[MagicMock]) -> MagicMock:
    by_id = {r.id: r for r in repos}
    session = MagicMock()
    session.get.side_effect = lambda _model, repo_id: by_id.get(repo_id)

    db = MagicMock()
    db.session.return_value.__enter__ = MagicMock(return_value=session)
    db.session.return_value.__exit__ = MagicMock(return_value=False)
    db.decrypt.side_effect = lambda enc: f"decrypted-{enc}"

    app = MagicMock()
    app.database = db
    return app


def _gitlab_app(
    repos: list[MagicMock],
    *,
    tree_bytes: dict[str, int | None],
    packed_bytes: dict[str, int] | None = None,
) -> MagicMock:
    app = _make_app(repos)
    packed = packed_bytes or {}
    app.gitlab.fetch_project = AsyncMock(
        side_effect=lambda _t, external_id, **_kw: {
            "statistics": {"repository_size": packed.get(external_id, 0)}
        }
    )
    app.gitlab.fetch_project_tree_bytes = AsyncMock(
        side_effect=lambda _t, external_id, *_a, **_kw: tree_bytes[external_id]
    )
    return app


# -- The formula ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkout_bytes_drive_the_size_not_the_packed_store():
    """A repo of compressible content: 100Mi packed, 5Gi checked out.

    Pricing off the packed store is what evicted a pod mid-run.
    """
    repo = _make_repo(external_id="1")
    app = _gitlab_app([repo], tree_bytes={"1": 5000 * MIB}, packed_bytes={"1": 100 * MIB})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(repo)

    assert result == f"{5000 + 100 + MARGIN_PER_REPO_MI}Mi"


@pytest.mark.asyncio
async def test_the_evicted_batch_now_fits():
    """The real group: 80Mi packed, 1407Mi on disk, evicted at a 1Gi limit."""
    repos = [_make_repo(external_id=str(i)) for i in range(1, 6)]
    disk = {"1": 35, "2": 4, "3": 2, "4": 1, "5": 1365}
    app = _gitlab_app(
        [*repos],
        tree_bytes={k: v * MIB for k, v in disk.items()},
        packed_bytes=dict.fromkeys(disk, 16 * MIB),
    )

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(repos[0], repos[1:])

    assert int(result.removesuffix("Mi")) >= 1407


@pytest.mark.asyncio
async def test_margin_scales_per_repo():
    """A flat margin spread across a group is what let the batch through."""
    a, b, c = (_make_repo(external_id=str(i)) for i in (1, 2, 3))
    big = 2000 * MIB
    app = _gitlab_app([a, b, c], tree_bytes={"1": big, "2": big, "3": big})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(a, [b, c])

    assert result == f"{6000 + MARGIN_PER_REPO_MI * 3}Mi"


@pytest.mark.asyncio
async def test_small_group_still_gets_the_floor():
    repo = _make_repo(external_id="1")
    app = _gitlab_app([repo], tree_bytes={"1": 5 * MIB})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        assert await resolve_tmp_size_limit(repo) == f"{FLOOR_MI}Mi"


@pytest.mark.asyncio
async def test_every_repo_in_the_group_is_priced():
    """Regression: only the first of four repos used to get a lookup."""
    repos = [_make_repo(external_id=str(i)) for i in range(1, 5)]
    app = _gitlab_app([*repos], tree_bytes={str(i): 1000 * MIB for i in range(1, 5)})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        await resolve_tmp_size_limit(repos[0], repos[1:])

    priced = {c.args[1] for c in app.gitlab.fetch_project_tree_bytes.await_args_list}
    assert priced == {"1", "2", "3", "4"}


@pytest.mark.asyncio
async def test_repo_listed_twice_is_counted_once():
    repo = _make_repo(external_id="1")
    app = _gitlab_app([repo], tree_bytes={"1": 4000 * MIB})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(repo, [repo])

    assert result == f"{4000 + MARGIN_PER_REPO_MI}Mi"


# -- Degradation ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_unavailable_tree_falls_back_to_packed_size(caplog):
    """Better a known-low estimate than none — but say so."""
    repo = _make_repo(external_id="1")
    app = _gitlab_app([repo], tree_bytes={"1": None}, packed_bytes={"1": 5000 * MIB})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(repo)

    assert result == f"{5000 + MARGIN_PER_REPO_MI}Mi"
    assert "checkout size unavailable" in caplog.text


@pytest.mark.asyncio
async def test_untokenised_repo_is_skipped_loudly(caplog):
    repo = _make_repo(external_id="1")
    app = _make_app([repo])

    with (
        patch(_GET_APP, return_value=app),
        patch(_ORG_BY_ID, return_value=_make_org(auth_token_encrypted=None)),
    ):
        result = await resolve_tmp_size_limit(repo)

    assert result == f"{FLOOR_MI}Mi"
    assert "no token" in caplog.text


@pytest.mark.asyncio
async def test_missing_statistics_is_reported_but_tree_still_counts(caplog):
    """An under-permissioned token omits `statistics`; the blobs still price."""
    repo = _make_repo(external_id="1")
    app = _make_app([repo])
    app.gitlab.fetch_project = AsyncMock(return_value={"id": 1})
    app.gitlab.fetch_project_tree_bytes = AsyncMock(return_value=4000 * MIB)

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(repo)

    assert result == f"{4000 + MARGIN_PER_REPO_MI}Mi"
    assert "no statistics" in caplog.text


@pytest.mark.asyncio
async def test_one_failing_repo_does_not_sink_the_group(caplog):
    """Sizing is an optimization — a raised lookup drops that repo only."""
    good, bad = _make_repo(external_id="1"), _make_repo(external_id="2")
    app = _make_app([good, bad])
    app.gitlab.fetch_project = AsyncMock(return_value={"statistics": {"repository_size": 0}})

    async def tree(_t, external_id, *_a, **_kw):
        if external_id == "2":
            raise RuntimeError("boom")
        return 4000 * MIB

    app.gitlab.fetch_project_tree_bytes = AsyncMock(side_effect=tree)

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        result = await resolve_tmp_size_limit(good, [bad])

    # The failed repo still earns its margin, just not its measured size.
    assert result == f"{4000 + MARGIN_PER_REPO_MI * 2}Mi"
    assert "size lookup failed" in caplog.text


@pytest.mark.asyncio
async def test_repo_token_is_preferred_over_its_org_token():
    """Cloning uses the repo-level token; sizing that reached for the org's
    instead priced such repos at zero."""
    repo = _make_repo(auth_token_encrypted="enc-repo", external_id="1")
    app = _gitlab_app([repo], tree_bytes={"1": 4000 * MIB})

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=_make_org()):
        await resolve_tmp_size_limit(repo)

    assert app.gitlab.fetch_project_tree_bytes.await_args.args[0] == "decrypted-enc-repo"


# -- GitHub --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_github_adds_tree_bytes_to_packed_size():
    repo = _make_repo(external_id="42")
    org = _make_org(provider="github")
    org.installation_id = "inst-1"
    app = _make_app([repo])
    app.github.get_installation_access_token = AsyncMock(return_value="tok")
    app.github.fetch_repository_by_id = AsyncMock(
        return_value={"size": 40 * 1024, "default_branch": "main"}
    )
    app.github.fetch_repo_tree_bytes = AsyncMock(return_value=3000 * MIB)

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=org):
        result = await resolve_tmp_size_limit(repo)

    assert result == f"{3000 + 40 + MARGIN_PER_REPO_MI}Mi"


@pytest.mark.asyncio
async def test_github_truncated_tree_falls_back_to_packed_size(caplog):
    """GitHub truncates past 100k entries; the sum would silently be short."""
    repo = _make_repo(external_id="42")
    org = _make_org(provider="github")
    org.installation_id = "inst-1"
    app = _make_app([repo])
    app.github.get_installation_access_token = AsyncMock(return_value="tok")
    app.github.fetch_repository_by_id = AsyncMock(
        return_value={"size": 5000 * 1024, "default_branch": "main"}
    )
    app.github.fetch_repo_tree_bytes = AsyncMock(return_value=None)

    with patch(_GET_APP, return_value=app), patch(_ORG_BY_ID, return_value=org):
        result = await resolve_tmp_size_limit(repo)

    assert result == f"{5000 + MARGIN_PER_REPO_MI}Mi"
    assert "checkout size unavailable" in caplog.text


# -- Batch resolution ----------------------------------------------------------


def _issue_on(mapped: MagicMock | None) -> MagicMock:
    issue = MagicMock()
    issue.repository = MagicMock() if mapped else None
    if mapped:
        issue.repository.mapped_repo = mapped
    return issue


def test_batch_resolves_every_distinct_mapped_repo():
    a, b = _make_repo(external_id="1"), _make_repo(external_id="2")
    assert [r.id for r in _resolve_mapped_repos([_issue_on(a), _issue_on(b)])] == [a.id, b.id]


def test_batch_dedupes_repeated_mapped_repos():
    a = _make_repo(external_id="1")
    assert [r.id for r in _resolve_mapped_repos([_issue_on(a)] * 3)] == [a.id]


def test_batch_skips_unmapped_issues():
    a = _make_repo(external_id="1")
    assert [r.id for r in _resolve_mapped_repos([_issue_on(None), _issue_on(a)])] == [a.id]
