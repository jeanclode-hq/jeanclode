"""Tests for repos listing, settings, and repo-group endpoints."""

from api.database import db_create_org, db_create_repository, db_create_workspace
from api.models.repositories import Repository
from tests.utils.access import grant_org_access


def test_list_repos(auth_client, app, mock_auth):
    """GET /repos returns connected repos for an organization."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="test-workspace", slug="test-workspace")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="test-org",
            external_org_id="123",
            installation_id="inst-123",
            provider="github",
            base_url="https://github.com",
        )
        # Create membership linking the auth user's identity to this org
        grant_org_access(db, org, mock_auth.id)
        db.commit()

        db_create_repository(
            db=db,
            org_id=org.id,
            external_id="789",
            name="my-repo",
            web_url="https://github.com/test-org/my-repo",
            provider="github",
            provider_url="https://github.com",
        )
        org_id = str(org.id)

    response = auth_client.get(f"/repos?org_id={org_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["has_more"] is False
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "my-repo"
    assert body["items"][0]["provider"] == "github"


def test_list_repos_pagination_and_search(auth_client, app, mock_auth):
    """GET /repos pages by ``page``/``limit`` and filters by ``search``."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="pg-ws", slug="pg-ws")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="pg-org",
            external_org_id="pg-1",
            installation_id="inst-pg",
            provider="github",
            base_url="https://github.com",
        )
        grant_org_access(db, org, mock_auth.id)
        db.commit()
        for i in range(30):
            db_create_repository(
                db=db,
                org_id=org.id,
                external_id=f"pg-{i}",
                name=f"repo-{i:02d}",
                web_url=f"https://github.com/pg-org/repo-{i:02d}",
                provider="github",
                provider_url="https://github.com",
            )
        org_id = str(org.id)

    page1 = auth_client.get(f"/repos?org_id={org_id}&page=1&limit=25").json()
    assert page1["total"] == 30
    assert page1["has_more"] is True
    assert [r["name"] for r in page1["items"]][:2] == ["repo-00", "repo-01"]
    assert len(page1["items"]) == 25

    page2 = auth_client.get(f"/repos?org_id={org_id}&page=2&limit=25").json()
    assert page2["has_more"] is False
    assert len(page2["items"]) == 5

    filtered = auth_client.get(f"/repos?org_id={org_id}&search=repo-1").json()
    assert filtered["total"] == 10
    assert all("repo-1" in r["name"] for r in filtered["items"])


def test_list_repos_empty(auth_client, app, mock_auth):
    """GET /repos for organization with no repos returns an empty page."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="empty-workspace", slug="empty-workspace")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="empty-org",
            external_org_id="999",
            installation_id="inst-999",
            provider="github",
            base_url="https://github.com",
        )
        grant_org_access(db, org, mock_auth.id)
        db.commit()
        org_id = str(org.id)

    response = auth_client.get(f"/repos?org_id={org_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["has_more"] is False


def test_list_repos_forbidden(auth_client, app, mock_auth):
    """GET /repos returns 403 for organization the user doesn't belong to."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="other-workspace", slug="other-workspace")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="other-org",
            external_org_id="888",
            installation_id="inst-888",
            provider="github",
            base_url="https://github.com",
        )
        org_id = str(org.id)

    response = auth_client.get(f"/repos?org_id={org_id}")
    assert response.status_code == 403


def _related_on_page(auth_client, org_id, repo_id):
    """A repo's group as the dashboard sees it — inlined on the repo list."""
    page = auth_client.get(f"/repos?org_id={org_id}&limit=200")
    assert page.status_code == 200
    row = next(r for r in page.json()["items"] if r["id"] == repo_id)
    return row["related"]


def _make_git_org_with_repos(app, mock_auth, *, names=("repo-a", "repo-b", "repo-c")):
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="group-ws", slug=f"group-ws-{names[0]}")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="group-org",
            external_org_id=f"grp-{names[0]}",
            installation_id="inst-grp",
            provider="github",
            base_url="https://github.com",
        )
        grant_org_access(db, org, mock_auth.id)
        db.commit()

        repo_ids = []
        for i, name in enumerate(names):
            repo = db_create_repository(
                db=db,
                org_id=org.id,
                external_id=f"ext-{names[0]}-{i}",
                name=name,
                web_url=f"https://github.com/group-org/{name}",
                provider="github",
                provider_url="https://github.com",
            )
            repo_ids.append(str(repo.id))
        return [*repo_ids, str(org.id)]


def _make_two_gitlab_orgs_one_workspace(app, user_id):
    """Two GitLab orgs (groups) sharing one workspace, each with one repo.

    The auth user is a member of the workspace. Returns (repo_a, repo_b).
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="xgroup-ws", slug="xgroup-ws")
        repo_ids = []
        for suffix in ("a", "b"):
            org = db_create_org(
                db=db,
                workspace_id=ws.id,
                name=f"group-{suffix}",
                external_org_id=f"group-{suffix}",
                provider="gitlab",
                base_url="https://gitlab.example.com",
            )
            grant_org_access(db, org, user_id)
            db.commit()
            repo = db_create_repository(
                db=db,
                org_id=org.id,
                external_id=f"ext-{suffix}",
                name=f"group-{suffix}/svc",
                web_url=f"https://gitlab.example.com/group-{suffix}/svc",
                provider="gitlab",
                provider_url="https://gitlab.example.com",
            )
            repo_ids.append(str(repo.id))
        return repo_ids[0], repo_ids[1]


def test_link_related_repo_cross_group_gitlab_same_workspace(auth_client, app, mock_auth):
    """GitLab repos in different groups of one workspace can be grouped."""
    repo_a, repo_b = _make_two_gitlab_orgs_one_workspace(app, mock_auth.id)

    response = auth_client.post(f"/repos/{repo_a}/related", json={"related_repo_id": repo_b})
    assert response.status_code == 200
    assert [r["name"] for r in response.json()] == ["group-b/svc"]


def test_link_related_repo_cross_org_github_rejected(auth_client, app, mock_auth):
    """GitHub repos in different orgs cannot be grouped — one token can't span orgs."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="gh-xorg-ws", slug="gh-xorg-ws")
        repo_ids = []
        for suffix in ("a", "b"):
            org = db_create_org(
                db=db,
                workspace_id=ws.id,
                name=f"gh-{suffix}",
                external_org_id=f"gh-{suffix}",
                installation_id=f"inst-{suffix}",
                provider="github",
                base_url="https://github.com",
            )
            grant_org_access(db, org, mock_auth.id)
            db.commit()
            repo = db_create_repository(
                db=db,
                org_id=org.id,
                external_id=f"gh-ext-{suffix}",
                name=f"gh-{suffix}/svc",
                web_url=f"https://github.com/gh-{suffix}/svc",
                provider="github",
                provider_url="https://github.com",
            )
            repo_ids.append(str(repo.id))

    response = auth_client.post(
        f"/repos/{repo_ids[0]}/related", json={"related_repo_id": repo_ids[1]}
    )
    assert response.status_code == 422


def test_link_related_repo_cross_workspace_forbidden(auth_client, app, mock_auth):
    """Linking is refused when the related repo's org sits in another workspace."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="halfaccess-ws", slug="halfaccess-ws")
        other_ws = db_create_workspace(db=db, name="halfaccess-other", slug="halfaccess-other")
        org_a = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="ha-a",
            external_org_id="ha-a",
            provider="gitlab",
            base_url="https://gitlab.example.com",
        )
        grant_org_access(db, org_a, mock_auth.id)
        db.commit()
        org_b = db_create_org(
            db=db,
            workspace_id=other_ws.id,
            name="ha-b",
            external_org_id="ha-b",
            provider="gitlab",
            base_url="https://gitlab.example.com",
        )
        db.commit()
        repo_a = db_create_repository(
            db=db,
            org_id=org_a.id,
            external_id="ha-ext-a",
            name="ha-a/svc",
            web_url="https://gitlab.example.com/ha-a/svc",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )
        repo_b = db_create_repository(
            db=db,
            org_id=org_b.id,
            external_id="ha-ext-b",
            name="ha-b/svc",
            web_url="https://gitlab.example.com/ha-b/svc",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )
        repo_a_id, repo_b_id = str(repo_a.id), str(repo_b.id)

    response = auth_client.post(f"/repos/{repo_a_id}/related", json={"related_repo_id": repo_b_id})
    assert response.status_code == 403


def test_link_related_repo(auth_client, app, mock_auth):
    """POST /repos/{id}/related links two repos and returns the updated group."""
    repo_a, repo_b, _, org_id = _make_git_org_with_repos(
        app, mock_auth, names=("link-a", "link-b", "link-c")
    )

    response = auth_client.post(f"/repos/{repo_a}/related", json={"related_repo_id": repo_b})
    assert response.status_code == 200
    assert [r["name"] for r in response.json()] == ["link-b"]

    # Readable from the other side too — storage is directional, the link isn't.
    assert [r["name"] for r in _related_on_page(auth_client, org_id, repo_b)] == ["link-a"]


def test_link_related_repo_reverse_is_not_duplicate(auth_client, app, mock_auth):
    """Linking (B, A) after (A, B) doesn't create a second edge."""
    repo_a, repo_b, _, _org = _make_git_org_with_repos(
        app, mock_auth, names=("rev-a", "rev-b", "rev-c")
    )

    auth_client.post(f"/repos/{repo_a}/related", json={"related_repo_id": repo_b})
    response = auth_client.post(f"/repos/{repo_b}/related", json={"related_repo_id": repo_a})
    assert response.status_code == 200
    assert len(response.json()) == 1


def test_unlink_related_repo(auth_client, app, mock_auth):
    """DELETE /repos/{id}/related/{other_id} removes the link."""
    repo_a, repo_b, _, org_id = _make_git_org_with_repos(
        app, mock_auth, names=("un-a", "un-b", "un-c")
    )

    auth_client.post(f"/repos/{repo_a}/related", json={"related_repo_id": repo_b})
    response = auth_client.delete(f"/repos/{repo_a}/related/{repo_b}")
    assert response.status_code == 200
    assert response.json() == []

    assert _related_on_page(auth_client, org_id, repo_b) == []


def _make_org(app, mock_auth, *, slug: str, repo_names):
    """One git org the auth user belongs to, holding ``repo_names``."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name=slug, slug=slug)
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name=slug,
            external_org_id=f"ext-{slug}",
            installation_id=f"inst-{slug}",
            provider="github",
            base_url="https://github.com",
        )
        grant_org_access(db, org, mock_auth.id)
        db.commit()
        for i, name in enumerate(repo_names):
            db_create_repository(
                db=db,
                org_id=org.id,
                external_id=f"{slug}-{i}",
                name=name,
                web_url=f"https://github.com/{slug}/{name}",
                provider="github",
                provider_url="https://github.com",
            )
        return str(org.id)


def test_list_repos_reports_enabled_count(auth_client, app, mock_auth):
    """Repos with no stored settings count as enabled; disabling one drops the count."""
    org_id = _make_org(app, mock_auth, slug="count-org", repo_names=("alpha", "beta"))

    body = auth_client.get(f"/repos?org_id={org_id}").json()
    assert body["enabled_count"] == 2

    repo_id = body["items"][0]["id"]
    assert (
        auth_client.patch(f"/repos/{repo_id}/settings", json={"enabled": False}).status_code == 200
    )

    assert auth_client.get(f"/repos?org_id={org_id}").json()["enabled_count"] == 1


def test_bulk_disable_then_enable_repos(auth_client, app, mock_auth):
    """PATCH /repos/settings flips every repo of the org, both directions."""
    org_id = _make_org(app, mock_auth, slug="bulk-org", repo_names=("alpha", "beta", "gamma"))

    response = auth_client.patch("/repos/settings", json={"org_id": org_id, "enabled": False})
    assert response.status_code == 200
    assert response.json()["updated"] == 3

    body = auth_client.get(f"/repos?org_id={org_id}").json()
    assert body["enabled_count"] == 0
    assert all(item["enabled"] is False for item in body["items"])

    assert (
        auth_client.patch("/repos/settings", json={"org_id": org_id, "enabled": True}).json()[
            "updated"
        ]
        == 3
    )
    assert auth_client.get(f"/repos?org_id={org_id}").json()["enabled_count"] == 3


def test_bulk_repo_settings_honours_search(auth_client, app, mock_auth):
    """``search`` scopes the bulk toggle to exactly the repos the filter shows."""
    org_id = _make_org(
        app, mock_auth, slug="filter-org", repo_names=("api-core", "api-edge", "web")
    )

    response = auth_client.patch(
        "/repos/settings", json={"org_id": org_id, "enabled": False, "search": "api"}
    )
    assert response.json()["updated"] == 2

    body = auth_client.get(f"/repos?org_id={org_id}").json()
    by_name = {item["name"]: item["enabled"] for item in body["items"]}
    assert by_name == {"api-core": False, "api-edge": False, "web": True}
    assert body["enabled_count"] == 1


def test_bulk_repo_settings_preserves_other_settings(auth_client, app, mock_auth):
    """The bulk write merges into the stored JSONB instead of replacing it."""
    org_id = _make_org(app, mock_auth, slug="merge-org", repo_names=("alpha",))

    with app.database.session() as db:
        repo = db.query(Repository).filter(Repository.name == "alpha").one()
        repo.settings = {"enabled": True, "custom": "keep-me"}
        db.commit()

    assert (
        auth_client.patch("/repos/settings", json={"org_id": org_id, "enabled": False}).status_code
        == 200
    )

    with app.database.session() as db:
        repo = db.query(Repository).filter(Repository.name == "alpha").one()
        assert repo.settings == {"enabled": False, "custom": "keep-me"}


def test_bulk_repo_settings_forbidden(auth_client, app, mock_auth):
    """A user with no membership in the org cannot bulk-toggle its repos."""
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="foreign-ws", slug="foreign-ws")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="foreign-org",
            external_org_id="ext-foreign",
            installation_id="inst-foreign",
            provider="github",
            base_url="https://github.com",
        )
        org_id = str(org.id)

    response = auth_client.patch("/repos/settings", json={"org_id": org_id, "enabled": False})
    assert response.status_code == 403


def test_disabled_repo_stays_in_its_group(auth_client, app, mock_auth):
    """Disabling a repo makes it inert as a trigger source, not as a group member.

    A run started from an enabled repo still clones its disabled partner, so
    ``db_get_related_repos`` must not filter on ``enabled``.
    """
    repo_a, repo_b, _, org_id = _make_git_org_with_repos(
        app, mock_auth, names=("group-lead", "group-follower", "spare")
    )
    auth_client.post(f"/repos/{repo_a}/related", json={"related_repo_id": repo_b})

    assert (
        auth_client.patch(f"/repos/{repo_b}/settings", json={"enabled": False}).status_code == 200
    )

    assert [r["id"] for r in _related_on_page(auth_client, org_id, repo_a)] == [repo_b]

    # The disabled partner still resolves at dispatch time, which is what the
    # group is for — the API shape above can't show that, so assert it here.
    with app.database.session() as db:
        from api.database import db_get_related_repos

        assert [str(r.id) for r in db_get_related_repos(db, repo_a)] == [repo_b]


def test_related_repos_report_the_root_org(auth_client, app, mock_auth):
    """A subgroup repo reports the connected group as its root org, not the subgroup.

    The dashboard labels a grouped repo by that root: subgroup orgs are never
    listed as connections, so keying on ``org_id`` leaves them unresolvable.
    """
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="root-ws", slug="root-ws")
        root = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="root-group",
            external_org_id="root-1",
            provider="gitlab",
            base_url="https://gitlab.example.com",
        )
        sub = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="atlas",
            external_org_id="sub-1",
            provider="gitlab",
            base_url="https://gitlab.example.com",
            parent_org_id=root.id,
            root_org_id=root.id,
        )
        grant_org_access(db, root, mock_auth.id)
        db.commit()

        top = db_create_repository(
            db=db,
            org_id=root.id,
            external_id="ext-top",
            name="root-group/api",
            web_url="https://gitlab.example.com/root-group/api",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )
        nested = db_create_repository(
            db=db,
            org_id=sub.id,
            external_id="ext-nested",
            name="root-group/atlas/ingester",
            web_url="https://gitlab.example.com/root-group/atlas/ingester",
            provider="gitlab",
            provider_url="https://gitlab.example.com",
        )
        root_id = str(root.id)
        top_id, nested_id = str(top.id), str(nested.id)

    response = auth_client.post(f"/repos/{top_id}/related", json={"related_repo_id": nested_id})
    assert response.status_code == 200
    related = response.json()[0]
    assert related["root_org_id"] == root_id

    page = auth_client.get(f"/repos?org_id={root_id}")
    assert page.json()["items"][0]["root_org_id"] == root_id
