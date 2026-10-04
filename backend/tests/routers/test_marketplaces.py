"""Tests for the plugin marketplace endpoints (6 routes)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_org, db_create_workspace
from api.routers.marketplaces.schemas import (
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
)
from tests.utils.access import grant_org_access

_LOAD = "api.routers.marketplaces.route.load_source_manifest"


@pytest.fixture
def org_with_membership(app, mock_auth):
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="ws", slug="ws")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="acme",
            external_org_id="acme-1",
            installation_id="inst-acme",
            provider="github",
            base_url="https://github.com",
        )
        grant_org_access(db, org, mock_auth.id)
        db.commit()
        return str(org.id)


def _manifest(*plugin_names: str) -> MarketplaceManifest:
    return MarketplaceManifest(
        name="acme marketplace",
        plugins=[
            MarketplacePlugin(name=n, description=f"desc {n}", source=f"plugins/{n}")
            for n in plugin_names
        ],
    )


# ---------------------------------------------------------------------------
# POST /marketplaces — connect
# ---------------------------------------------------------------------------


def test_connect_marketplace(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1", "p2"),
    ):
        r = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        )
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "acme marketplace"
    assert data["last_sync_status"] == "ok"
    assert [p["name"] for p in data["plugins"]] == ["p1", "p2"]


def test_connect_marketplace_fetch_error(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        side_effect=MarketplaceFetchError("boom"),
    ):
        r = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/bad"},
        )
    assert r.status_code == 400


def test_connect_marketplace_duplicate(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("a"),
    ):
        auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        )
        r = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# GET /plugins
# ---------------------------------------------------------------------------


def test_overview_empty(auth_client, org_with_membership):
    r = auth_client.get(f"/plugins?org_id={org_with_membership}")
    assert r.json() == {"marketplaces": [], "installed": []}


def test_overview_inlines_manifest(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1", "p2"),
    ):
        connect = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        )
        mid = connect.json()["id"]

        auth_client.post(
            "/plugins",
            json={
                "org_id": org_with_membership,
                "marketplace_id": mid,
                "plugin_names": ["p1"],
            },
        )

        r = auth_client.get(f"/plugins?org_id={org_with_membership}")

    body = r.json()
    assert len(body["marketplaces"]) == 1
    by_name = {p["name"]: p for p in body["marketplaces"][0]["plugins"]}
    assert by_name["p1"]["installed"] is True
    assert by_name["p2"]["installed"] is False
    assert len(body["installed"]) == 1


# ---------------------------------------------------------------------------
# POST /plugins — install from marketplace
# ---------------------------------------------------------------------------


def test_unsupported_source_is_flagged_and_refused(auth_client, org_with_membership):
    manifest = MarketplaceManifest(
        name="m",
        plugins=[
            MarketplacePlugin(name="ok", source="./ok"),
            MarketplacePlugin(name="pkg", source={"source": "npm", "package": "@a/pkg"}),
        ],
    )
    with patch("api.routers.marketplaces.route.load_source_manifest", return_value=manifest):
        connect = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        )
        mid = connect.json()["id"]
        r = auth_client.post(
            "/plugins",
            json={"org_id": org_with_membership, "marketplace_id": mid, "plugin_names": ["pkg"]},
        )

    reasons = {p["name"]: p["unsupported_reason"] for p in connect.json()["plugins"]}
    assert reasons["ok"] is None
    assert "'npm' is not supported" in reasons["pkg"]
    assert r.status_code == 422
    assert "pkg" in r.json()["detail"]


def test_install_unknown_plugin_skipped(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("only"),
    ):
        mid = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        ).json()["id"]

        r = auth_client.post(
            "/plugins",
            json={
                "org_id": org_with_membership,
                "marketplace_id": mid,
                "plugin_names": ["missing"],
            },
        )
    assert r.status_code == 201
    assert r.json() == []


def test_install_duplicate_skipped(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1"),
    ):
        mid = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        ).json()["id"]

        payload = {
            "org_id": org_with_membership,
            "marketplace_id": mid,
            "plugin_names": ["p1"],
        }
        first = auth_client.post("/plugins", json=payload)
        assert len(first.json()) == 1

        second = auth_client.post("/plugins", json=payload)
        assert second.status_code == 201
        assert second.json() == []


def test_install_batch(auth_client, org_with_membership):
    """Install multiple plugins in a single call."""
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1", "p2", "p3"),
    ):
        mid = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        ).json()["id"]

        r = auth_client.post(
            "/plugins",
            json={
                "org_id": org_with_membership,
                "marketplace_id": mid,
                "plugin_names": ["p1", "p2", "p3"],
            },
        )
    assert r.status_code == 201
    assert len(r.json()) == 3


# ---------------------------------------------------------------------------
# PATCH / DELETE plugin
# ---------------------------------------------------------------------------


def test_update_install(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1"),
    ):
        mid = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        ).json()["id"]
        install = auth_client.post(
            "/plugins",
            json={
                "org_id": org_with_membership,
                "marketplace_id": mid,
                "plugin_names": ["p1"],
            },
        ).json()[0]

    r = auth_client.patch(
        f"/plugins/{install['id']}",
        json={
            "pinned_ref": "v2",
            "enabled_workflows": ["fix"],
            "project_overrides": {"r1": {"enabled": False}},
        },
    )
    assert r.status_code == 200
    assert r.json()["pinned_ref"] == "v2"


def test_uninstall(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1"),
    ):
        mid = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        ).json()["id"]
        install = auth_client.post(
            "/plugins",
            json={
                "org_id": org_with_membership,
                "marketplace_id": mid,
                "plugin_names": ["p1"],
            },
        ).json()[0]

    assert auth_client.delete(f"/plugins/{install['id']}").status_code == 204
    r = auth_client.get(f"/plugins?org_id={org_with_membership}")
    assert r.json()["installed"] == []


def test_disconnect_cascades(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route.load_source_manifest",
        return_value=_manifest("p1"),
    ):
        mid = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/m"},
        ).json()["id"]
        auth_client.post(
            "/plugins",
            json={
                "org_id": org_with_membership,
                "marketplace_id": mid,
                "plugin_names": ["p1"],
            },
        )

    assert auth_client.delete(f"/marketplaces/{mid}").status_code == 204
    r = auth_client.get(f"/plugins?org_id={org_with_membership}")
    assert r.json() == {"marketplaces": [], "installed": []}


def test_install_forbidden_for_other_org(auth_client, app, mock_auth):
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="other", slug="other")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="other",
            external_org_id="other-1",
            installation_id="inst-other",
            provider="github",
            base_url="https://github.com",
        )
        other_id = str(org.id)

    r = auth_client.post(
        "/plugins",
        json={
            "org_id": other_id,
            "marketplace_id": "00000000-0000-0000-0000-000000000000",
            "plugin_names": ["p"],
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Skill sources beyond marketplace.json
# ---------------------------------------------------------------------------


def _skills_manifest(*names: str) -> MarketplaceManifest:
    return MarketplaceManifest(
        name="acme/skills",
        kind="skills",
        plugins=[
            MarketplacePlugin(
                name=n, description=f"desc {n}", source="./", skills=[f"./skills/{n}"]
            )
            for n in names
        ],
    )


@pytest.mark.parametrize(
    ("pasted", "stored"),
    [
        ("acme/skills", "https://github.com/acme/skills"),
        ("https://github.com/acme/skills.git", "https://github.com/acme/skills"),
        ("git@github.com:acme/skills.git", "https://github.com/acme/skills"),
        (
            "https://github.com/acme/skills/tree/main/skills/pdf",
            "https://github.com/acme/skills/tree/main/skills/pdf",
        ),
        (
            "https://gitlab.example.org/g/sub/p/-/blob/v1/.claude/skills/x/SKILL.md",
            "https://gitlab.example.org/g/sub/p/-/tree/v1/.claude/skills/x",
        ),
    ],
)
def test_connect_stores_the_canonical_url(auth_client, org_with_membership, pasted, stored):
    load = AsyncMock(return_value=_skills_manifest("pdf"))
    with patch(_LOAD, new=load):
        r = auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": pasted}
        )

    assert r.status_code == 201, r.text
    assert r.json()["git_url"] == stored
    assert load.await_args.args[0] == stored
    assert load.await_args.kwargs["describe"] is True


def test_connect_returns_the_source_kind_and_skill_descriptions(auth_client, org_with_membership):
    with patch(_LOAD, new=AsyncMock(return_value=_skills_manifest("pdf", "xlsx"))):
        body = auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": "acme/skills"}
        ).json()

    assert body["kind"] == "skills"
    assert body["name"] == "acme/skills"
    assert [(p["name"], p["description"], p["unsupported_reason"]) for p in body["plugins"]] == [
        ("pdf", "desc pdf", None),
        ("xlsx", "desc xlsx", None),
    ]


@pytest.mark.parametrize("pasted", ["", "not a url", "http://github.com/a/b", "file:///tmp/x"])
def test_connect_rejects_what_it_cannot_parse_without_fetching(
    auth_client, org_with_membership, pasted
):
    load = AsyncMock()
    with patch(_LOAD, new=load):
        r = auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": pasted}
        )

    assert r.status_code == 400
    load.assert_not_awaited()


@pytest.mark.parametrize("second", ["acme/skills", "https://github.com/acme/skills.git"])
def test_connect_dedupes_on_the_canonical_url(auth_client, org_with_membership, second):
    with patch(_LOAD, new=AsyncMock(return_value=_skills_manifest("pdf"))):
        auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/skills"},
        )
        r = auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": second}
        )
    assert r.status_code == 409


def test_connect_surfaces_the_loader_error(auth_client, org_with_membership):
    err = MarketplaceFetchError("no .claude-plugin/marketplace.json and no SKILL.md found")
    with patch(_LOAD, new=AsyncMock(side_effect=err)):
        r = auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": "acme/empty"}
        )
    assert r.status_code == 400
    assert "no SKILL.md" in r.json()["detail"]


def test_install_a_discovered_skill(auth_client, org_with_membership):
    with patch(_LOAD, new=AsyncMock(return_value=_skills_manifest("pdf", "xlsx"))) as load:
        mid = auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": "acme/skills"}
        ).json()["id"]
        r = auth_client.post(
            "/plugins",
            json={"org_id": org_with_membership, "marketplace_id": mid, "plugin_names": ["pdf"]},
        )
        overview = auth_client.get(f"/plugins?org_id={org_with_membership}").json()

    assert r.status_code == 201
    assert [p["plugin_name"] for p in r.json()] == ["pdf"]
    assert load.await_args_list[1].kwargs.get("describe", False) is False
    [market] = overview["marketplaces"]
    assert market["kind"] == "skills"
    assert {p["name"]: p["installed"] for p in market["plugins"]} == {"pdf": True, "xlsx": False}


def test_overview_kind_is_null_when_the_source_is_unreachable(auth_client, org_with_membership):
    with patch(_LOAD, new=AsyncMock(return_value=_skills_manifest("pdf"))):
        auth_client.post(
            "/marketplaces", json={"org_id": org_with_membership, "git_url": "acme/skills"}
        )
    with patch(_LOAD, new=AsyncMock(side_effect=MarketplaceFetchError("gone"))):
        [market] = auth_client.get(f"/plugins?org_id={org_with_membership}").json()["marketplaces"]

    assert (market["kind"], market["plugins"], market["last_sync_status"]) == (None, None, "error")
    assert market["last_sync_error"] == "gone"


def test_loader_gets_the_org_token_tagged_with_its_host(auth_client, app, mock_auth):
    from api.database import db_create_org, db_create_workspace
    from tests.utils.access import grant_org_access

    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="ws2", slug="ws2")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="grp",
            external_org_id="grp",
            provider="gitlab",
            base_url="https://gitlab.example.org",
            auth_token_encrypted=app.database.encrypt("glpat-secret"),
        )
        grant_org_access(db, org, mock_auth.id)
        db.commit()
        org_id = str(org.id)

    load = AsyncMock(return_value=_skills_manifest("happily"))
    with patch(_LOAD, new=load):
        r = auth_client.post(
            "/marketplaces",
            json={"org_id": org_id, "git_url": "https://gitlab.example.org/guild-backend/happily"},
        )

    assert r.status_code == 201, r.text
    auth = load.await_args.kwargs["auth"]
    assert (auth.provider, auth.token, auth.base_url) == (
        "gitlab",
        "glpat-secret",
        "https://gitlab.example.org",
    )
