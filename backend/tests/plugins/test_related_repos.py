"""Which repos join a run's workspace: groups, always-include, subgroup pack."""

import uuid

from sqlalchemy.orm import Session

from api.database.repository import db_link_repos
from api.models.organizations import Organization
from api.models.repositories import Repository
from api.models.workspaces import Workspace
from api.plugins.container.related import MAX_PACKED_SUBGROUP_REPOS, resolve_related_repos


def _make_tree(db: Session, settings: dict | None = None) -> tuple[Organization, Organization]:
    """A connected root org with one subgroup under it."""
    ws = Workspace(name="ws", slug=f"ws-{uuid.uuid4().hex[:6]}")
    db.add(ws)
    db.flush()
    root = Organization(
        workspace_id=ws.id,
        name="team-uep",
        external_org_id=f"gl-{uuid.uuid4().hex[:6]}",
        provider="gitlab",
        settings=settings or {},
    )
    db.add(root)
    db.flush()
    subgroup = Organization(
        workspace_id=ws.id,
        name="junoinstance",
        external_org_id=f"gl-{uuid.uuid4().hex[:6]}",
        provider="gitlab",
        parent_org_id=root.id,
        root_org_id=root.id,
    )
    db.add(subgroup)
    db.commit()
    return root, subgroup


def _make_repo(db: Session, org: Organization, name: str, **settings) -> Repository:
    repo = Repository(
        org_id=org.id,
        name=name,
        external_id=f"ext-{uuid.uuid4().hex[:6]}",
        provider="gitlab",
        web_url=f"https://gitlab.example.com/{name}",
        settings=settings,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)
    return repo


def test_pack_pulls_subgroup_siblings_but_not_the_repo_itself(db_session: Session) -> None:
    _, subgroup = _make_tree(db_session)
    spec = _make_repo(db_session, subgroup, "junoinstance/project-specification")
    juno = _make_repo(db_session, subgroup, "junoinstance/juno")

    assert [r.id for r in resolve_related_repos(db_session, spec)] == [juno.id]


def test_pack_stops_at_the_subgroup_boundary(db_session: Session) -> None:
    root, subgroup = _make_tree(db_session)
    spec = _make_repo(db_session, subgroup, "junoinstance/project-specification")
    _make_repo(db_session, root, "team-uep/unrelated")

    assert resolve_related_repos(db_session, spec) == []


def test_repo_on_the_connected_root_never_packs(db_session: Session) -> None:
    """The root is not a subgroup — packing there would mean the whole connection."""
    root, _ = _make_tree(db_session)
    one = _make_repo(db_session, root, "team-uep/one")
    _make_repo(db_session, root, "team-uep/two")

    assert resolve_related_repos(db_session, one) == []


def test_pack_is_skipped_whole_above_the_cap(db_session: Session) -> None:
    _, subgroup = _make_tree(db_session)
    primary = _make_repo(db_session, subgroup, "old-dc-qa/primary")
    for i in range(MAX_PACKED_SUBGROUP_REPOS):
        _make_repo(db_session, subgroup, f"old-dc-qa/repo-{i:03d}")

    assert resolve_related_repos(db_session, primary) == []


def test_pack_skips_disabled_repos(db_session: Session) -> None:
    _, subgroup = _make_tree(db_session)
    primary = _make_repo(db_session, subgroup, "junoinstance/project-specification")
    _make_repo(db_session, subgroup, "junoinstance/archived", enabled=False)

    assert resolve_related_repos(db_session, primary) == []


def test_excluded_subgroup_does_not_pack(db_session: Session) -> None:
    root, subgroup = _make_tree(db_session)
    root.settings = {"related_repos": {"excluded_subgroups": [str(subgroup.id)]}}
    db_session.commit()
    primary = _make_repo(db_session, subgroup, "junoinstance/project-specification")
    _make_repo(db_session, subgroup, "junoinstance/juno")

    assert resolve_related_repos(db_session, primary) == []


def test_pack_can_be_turned_off_on_the_connected_group(db_session: Session) -> None:
    root, subgroup = _make_tree(db_session)
    root.settings = {"related_repos": {"pack_subgroup": False}}
    db_session.commit()
    primary = _make_repo(db_session, subgroup, "junoinstance/project-specification")
    _make_repo(db_session, subgroup, "junoinstance/juno")

    assert resolve_related_repos(db_session, primary) == []


def test_always_include_is_one_directional(db_session: Session) -> None:
    """Work on D → bring C. Work on C → bring nothing."""
    root, subgroup = _make_tree(db_session)
    pinned = _make_repo(db_session, subgroup, "junoinstance/juno")
    other = _make_repo(db_session, root, "team-uep/backend")
    root.settings = {"related_repos": {"always_include": [str(pinned.id)], "pack_subgroup": False}}
    db_session.commit()

    assert [r.id for r in resolve_related_repos(db_session, other)] == [pinned.id]
    assert resolve_related_repos(db_session, pinned) == []


def test_always_include_reaches_repos_in_another_subgroup(db_session: Session) -> None:
    root, subgroup = _make_tree(db_session)
    pinned = _make_repo(db_session, subgroup, "junoinstance/juno")
    root.settings = {"related_repos": {"always_include": [str(pinned.id)], "pack_subgroup": False}}
    db_session.commit()
    sibling_org = Organization(
        workspace_id=root.workspace_id,
        name="tools",
        external_org_id=f"gl-{uuid.uuid4().hex[:6]}",
        provider="gitlab",
        parent_org_id=root.id,
        root_org_id=root.id,
    )
    db_session.add(sibling_org)
    db_session.commit()
    elsewhere = _make_repo(db_session, sibling_org, "tools/cli")

    assert [r.id for r in resolve_related_repos(db_session, elsewhere)] == [pinned.id]


def test_explicit_group_survives_the_cap(db_session: Session) -> None:
    """A hand-made mapping is a deliberate choice — only the pack is capped."""
    _, subgroup = _make_tree(db_session)
    primary = _make_repo(db_session, subgroup, "old-dc-qa/primary")
    linked = _make_repo(db_session, subgroup, "old-dc-qa/linked")
    for i in range(MAX_PACKED_SUBGROUP_REPOS):
        _make_repo(db_session, subgroup, f"old-dc-qa/repo-{i:03d}")
    db_link_repos(db_session, primary.id, linked.id)

    assert [r.id for r in resolve_related_repos(db_session, primary)] == [linked.id]


def test_sources_are_deduped(db_session: Session) -> None:
    root, subgroup = _make_tree(db_session)
    primary = _make_repo(db_session, subgroup, "junoinstance/project-specification")
    juno = _make_repo(db_session, subgroup, "junoinstance/juno")
    root.settings = {"related_repos": {"always_include": [str(juno.id)]}}
    db_session.commit()
    db_link_repos(db_session, primary.id, juno.id)

    assert [r.id for r in resolve_related_repos(db_session, primary)] == [juno.id]


def test_no_repo_resolves_to_nothing(db_session: Session) -> None:
    assert resolve_related_repos(db_session, None) == []
