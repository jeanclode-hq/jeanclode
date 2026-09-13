"""How many pooled connections one dashboard request is allowed to occupy.

The number the pool can serve concurrently is (pool_size + max_overflow)
divided by whatever a single request holds at once, so a handler that quietly
takes two — its own session plus one opened by an access check — halves how
many people can use the dashboard before ``QueuePool limit ... reached``.
These tests pin the count at one.
"""

from sqlalchemy import event

from api.database import db_create_org, db_create_repository, db_create_workspace
from tests.utils.access import grant_org_access


def _peak_checked_out(app, fn):
    """Run ``fn`` and report the most connections checked out at any moment."""
    engine = app.database.engine
    live = 0
    peak = 0

    def on_checkout(*_args):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)

    def on_checkin(*_args):
        nonlocal live
        live -= 1

    event.listen(engine, "checkout", on_checkout)
    event.listen(engine, "checkin", on_checkin)
    try:
        fn()
    finally:
        event.remove(engine, "checkout", on_checkout)
        event.remove(engine, "checkin", on_checkin)
    return peak


def _org_with_repo(app, user_id):
    with app.database.session() as db:
        ws = db_create_workspace(db=db, name="pool-ws", slug="pool-ws")
        org = db_create_org(
            db=db,
            workspace_id=ws.id,
            name="pool-org",
            external_org_id="pool-1",
            installation_id="inst-pool",
            provider="github",
            base_url="https://github.com",
        )
        grant_org_access(db, org, user_id)
        db.commit()
        repo = db_create_repository(
            db=db,
            org_id=org.id,
            external_id="pool-ext-1",
            name="pool-org/api",
            web_url="https://github.com/pool-org/api",
            provider="github",
            provider_url="https://github.com",
        )
        return str(org.id), str(repo.id)


def test_repo_list_holds_one_connection(auth_client, app, mock_auth):
    org_id, _ = _org_with_repo(app, mock_auth.id)

    peak = _peak_checked_out(
        app, lambda: auth_client.get(f"/repos?org_id={org_id}").raise_for_status()
    )
    assert peak == 1


def test_repo_settings_write_holds_one_connection(auth_client, app, mock_auth):
    """The path that fell over in production: an org access check mid-handler."""
    _, repo_id = _org_with_repo(app, mock_auth.id)

    peak = _peak_checked_out(
        app,
        lambda: auth_client.patch(
            f"/repos/{repo_id}/settings", json={"enabled": False}
        ).raise_for_status(),
    )
    assert peak == 1
