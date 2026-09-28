"""Repository lookup by external id across providers and instances."""

import uuid

from sqlalchemy.orm import Session

from api.database.repository import db_delete_repository, db_get_repository_by_external_id
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.models.workspaces import Workspace


def _repo(db: Session, *, provider: str, base_url: str | None, name: str) -> Repository:
    workspace = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(workspace)
    db.flush()
    org = Organization(
        workspace_id=workspace.id,
        name=name,
        external_org_id=f"org-{uuid.uuid4().hex[:6]}",
        provider=provider,
        base_url=base_url,
    )
    db.add(org)
    db.flush()
    repo = Repository(org_id=org.id, external_id="469", name=name, provider=provider)
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def test_same_id_on_sentry_and_gitlab_resolves_by_provider(db_session):
    sentry = _repo(
        db_session, provider="sentry", base_url="https://sentry.example.net", name="sentry"
    )
    gitlab = _repo(
        db_session, provider="gitlab", base_url="https://gitlab.example.in", name="gitlab"
    )

    found = db_get_repository_by_external_id(
        db_session, "469", provider="gitlab", host="https://gitlab.example.in/group/app"
    )
    assert found.id == gitlab.id
    assert db_get_repository_by_external_id(db_session, "469", provider="sentry").id == sentry.id


def test_same_id_on_two_gitlab_instances_resolves_by_host(db_session):
    prod = _repo(db_session, provider="gitlab", base_url="https://gitlab.example.in", name="in")
    dev = _repo(db_session, provider="gitlab", base_url="https://gitlab.example.dev", name="dev")
    saas = _repo(db_session, provider="gitlab", base_url=None, name="saas")

    lookup = db_get_repository_by_external_id
    assert lookup(db_session, "469", provider="gitlab", host="gitlab.example.dev").id == dev.id
    assert lookup(db_session, "469", provider="gitlab", host="https://gitlab.example.in/").id == (
        prod.id
    )
    assert lookup(db_session, "469", provider="gitlab", host="https://gitlab.com/a/b").id == saas.id
    assert lookup(db_session, "469", provider="gitlab", host="gitlab.other.org") is None


def test_delete_only_touches_the_matching_provider(db_session):
    sentry = _repo(db_session, provider="sentry", base_url=None, name="sentry")
    _repo(db_session, provider="gitlab", base_url="https://gitlab.example.in", name="gitlab")

    assert db_delete_repository(db_session, "469", provider="gitlab", host="gitlab.example.in")
    assert db_get_repository_by_external_id(db_session, "469", provider="gitlab") is None
    assert db_get_repository_by_external_id(db_session, "469", provider="sentry").id == sentry.id
