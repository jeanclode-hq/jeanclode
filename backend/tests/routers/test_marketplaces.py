"""Tests for the plugin marketplace endpoints (6 routes)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from api.database import db_create_org, db_create_workspace
from api.routers.marketplaces.schemas import (
    MarketplaceFetchError,
    MarketplaceManifest,
    MarketplacePlugin,
)
from tests.utils.access import grant_org_access


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
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
        side_effect=MarketplaceFetchError("boom"),
    ):
        r = auth_client.post(
            "/marketplaces",
            json={"org_id": org_with_membership, "git_url": "https://github.com/acme/bad"},
        )
    assert r.status_code == 400


def test_connect_marketplace_duplicate(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
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


def test_install_unknown_plugin_skipped(auth_client, org_with_membership):
    with patch(
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
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
        "api.routers.marketplaces.route._fetch_manifest",
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
