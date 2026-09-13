"""Tests for the internal memory endpoints (issue #180) and their real,
per-execution token auth (issue #181).

Exercises the six memory_20250818-contract commands (view/create/
str_replace/insert/delete/rename) against the real DB-backed router, plus
the workspace-scoping, path-traversal, and size/count-cap hygiene.

Auth uses the real ``get_memory_principal`` dependency (see
``api.routers.internal.dependencies``): a signed, expiring, per-execution
token minted by ``api.services.memory_token.mint_memory_token``, keyed off
the signing secret the backend auto-generates and persists on first use
(``get_or_create_memory_signing_secret``). A fixed value is pre-seeded here
so tests can mint tokens without depending on generation order.
"""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from api.database import db_create_workspace
from api.database.instance_settings import db_upsert_setting
from api.database.memory import db_count_memory_entries, db_create_memory_entry
from api.services.instance_settings import MEMORY_SIGNING_SECRET_KEY
from api.services.memory_token import mint_memory_token

TEST_SECRET = "test-memory-signing-secret-do-not-use-in-prod"


@pytest.fixture
def memory_client(app, db_session) -> TestClient:
    web = app.web
    assert web is not None
    db_upsert_setting(
        db_session, "memory", MEMORY_SIGNING_SECRET_KEY, app.database.encrypt(TEST_SECRET)
    )
    db_session.commit()
    return TestClient(web.get_asgi_app())


@pytest.fixture
def workspace_id(db_session) -> uuid.UUID:
    ws = db_create_workspace(
        db=db_session, name="memory-ws", slug=f"memory-ws-{uuid.uuid4().hex[:8]}"
    )
    return ws.id


def _auth(workspace_id: uuid.UUID, secret: str = TEST_SECRET) -> dict[str, str]:
    token = mint_memory_token(workspace_id=workspace_id, execution_id=uuid.uuid4(), secret=secret)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_view_missing_auth_header_401(memory_client, workspace_id):
    resp = memory_client.get("/internal/memory/view")
    assert resp.status_code == 401


def test_view_wrong_secret_401(memory_client, workspace_id):
    """A token signed with a different secret fails signature verification."""
    resp = memory_client.get("/internal/memory/view", headers=_auth(workspace_id, secret="wrong"))
    assert resp.status_code == 401


def test_view_tampered_token_401(memory_client, workspace_id):
    """Flipping a single character anywhere in a valid token invalidates it."""
    token = _auth(workspace_id)["Authorization"].removeprefix("Bearer ")
    tampered_char = "a" if token[-1] != "a" else "b"
    tampered = token[:-1] + tampered_char
    resp = memory_client.get(
        "/internal/memory/view", headers={"Authorization": f"Bearer {tampered}"}
    )
    assert resp.status_code == 401


def test_view_expired_token_401(memory_client, workspace_id):
    """A token minted with an already-past expiry is rejected."""
    from datetime import timedelta

    token = mint_memory_token(
        workspace_id=workspace_id,
        execution_id=uuid.uuid4(),
        secret=TEST_SECRET,
        ttl=timedelta(seconds=-1),
    )
    resp = memory_client.get("/internal/memory/view", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_view_cross_workspace_token_never_resolves_other_workspace(memory_client, db_session):
    """A token minted for workspace A always resolves to A, never any other
    workspace — the token itself is the only source of workspace_id, and a
    forged/edited claim fails signature verification rather than silently
    granting scope to whatever workspace_id an attacker writes in."""
    ws_a = db_create_workspace(db=db_session, name="ws-a", slug=f"ws-a-{uuid.uuid4().hex[:8]}")
    ws_b = db_create_workspace(db=db_session, name="ws-b", slug=f"ws-b-{uuid.uuid4().hex[:8]}")

    token_a = mint_memory_token(workspace_id=ws_a.id, execution_id=uuid.uuid4(), secret=TEST_SECRET)

    # Using ws A's token still only ever grants ws A's data...
    memory_client.post(
        "/internal/memory/create",
        json={"path": "a.md", "content": "belongs to A"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    resp = memory_client.get(
        "/internal/memory/view",
        params={"path": "a.md"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 200

    # ...and can never be presented to read workspace B's data (never
    # created there in the first place — this is the workspace-scoping
    # invariant, not a forgery of the token itself).
    resp = memory_client.get(
        "/internal/memory/view",
        params={"path": "a.md"},
        headers=_auth(ws_b.id),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


def test_view_empty_workspace_root_returns_empty_listing_not_error(memory_client, workspace_id):
    resp = memory_client.get("/internal/memory/view", headers=_auth(workspace_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_directory"] is True
    assert body["entries"] == []


def test_view_missing_path_404(memory_client, workspace_id):
    resp = memory_client.get(
        "/internal/memory/view", params={"path": "nope.md"}, headers=_auth(workspace_id)
    )
    assert resp.status_code == 404


def test_view_file_returns_raw_content(memory_client, workspace_id):
    memory_client.post(
        "/internal/memory/create",
        json={"path": "notes.md", "content": "hello world"},
        headers=_auth(workspace_id),
    )
    resp = memory_client.get(
        "/internal/memory/view", params={"path": "notes.md"}, headers=_auth(workspace_id)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_directory"] is False
    assert body["content"] == "hello world"


def test_view_directory_listing_two_levels_deep(memory_client, workspace_id):
    for path in ["a/b/c/d.md", "a/x.md", "top.md"]:
        memory_client.post(
            "/internal/memory/create",
            json={"path": path, "content": "x"},
            headers=_auth(workspace_id),
        )
    resp = memory_client.get("/internal/memory/view", headers=_auth(workspace_id))
    assert resp.status_code == 200
    entries = {e["path"]: e["size"] for e in resp.json()["entries"]}
    # top.md and a/x.md are within 2 levels -> listed directly.
    assert entries["top.md"] == 1
    assert entries["a/x.md"] == 1
    # a/b/c/d.md is 3 levels deep -> folded into a depth-2 directory marker.
    assert "a/b/" in entries
    assert entries["a/b/"] is None
    assert "a/b/c/d.md" not in entries


def test_view_is_scoped_per_workspace(memory_client, db_session):
    ws1 = db_create_workspace(db=db_session, name="ws1", slug=f"ws1-{uuid.uuid4().hex[:8]}")
    ws2 = db_create_workspace(db=db_session, name="ws2", slug=f"ws2-{uuid.uuid4().hex[:8]}")
    memory_client.post(
        "/internal/memory/create",
        json={"path": "secret.md", "content": "ws1 only"},
        headers=_auth(ws1.id),
    )
    resp = memory_client.get(
        "/internal/memory/view", params={"path": "secret.md"}, headers=_auth(ws2.id)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_success(memory_client, workspace_id):
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": "todo.md", "content": "- [ ] thing", "description": "todos"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["path"] == "todo.md"
    assert body["content"] == "- [ ] thing"
    assert body["name"] == "todo.md"
    assert body["description"] == "todos"


def test_create_rejects_path_already_used_as_directory(memory_client, workspace_id):
    """A file can't be created at a path another entry already uses as a
    prefix — otherwise view() on the file would permanently hide it."""
    memory_client.post(
        "/internal/memory/create",
        json={"path": "notes/x.md", "content": "x"},
        headers=_auth(workspace_id),
    )
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": "notes", "content": "y"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400


def test_create_rejects_ancestor_already_a_file(memory_client, workspace_id):
    """The reverse collision: an ancestor segment already exists as a file."""
    memory_client.post(
        "/internal/memory/create",
        json={"path": "notes", "content": "x"},
        headers=_auth(workspace_id),
    )
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": "notes/x.md", "content": "y"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "ancestor" in resp.json()["detail"]


def test_create_duplicate_path_error(memory_client, db_session, workspace_id):
    memory_client.post(
        "/internal/memory/create",
        json={"path": "dup.md", "content": "first"},
        headers=_auth(workspace_id),
    )
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": "dup.md", "content": "second"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Error: File dup.md already exists"

    # No duplicate row got through.
    assert db_count_memory_entries(db_session, workspace_id) == 1


def test_create_concurrent_same_path_race_is_db_enforced(app, db_session, workspace_id):
    """Two concurrent creates at the same path: one succeeds, one fails
    cleanly with IntegrityError, and exactly one row exists — proving the
    unique constraint (not app-level check-then-insert) decides the race.
    """
    db_plugin = app.database
    assert db_plugin is not None

    results: list[str] = []

    def _attempt():
        try:
            with db_plugin.session() as db:
                db_create_memory_entry(
                    db,
                    workspace_id=workspace_id,
                    path="race.md",
                    content="x",
                    name="race.md",
                )
            results.append("ok")
        except IntegrityError:
            results.append("conflict")

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["conflict", "ok"]
    assert db_count_memory_entries(db_session, workspace_id) == 1


def test_create_concurrent_same_path_race_through_the_real_http_route(
    app, memory_client, db_session, workspace_id
):
    """Same race as above, but driven through two real concurrent HTTP
    requests against POST /internal/memory/create — exercises the route's
    own try/except IntegrityError translation (route.py), not just the DB
    function it calls. One request gets 201, the other gets exactly the
    documented 400 "already exists" error, and only one row ever exists.
    """
    web = app.web
    assert web is not None
    results: list[tuple[int, dict]] = []

    def _attempt():
        client = TestClient(web.get_asgi_app())
        resp = client.post(
            "/internal/memory/create",
            json={"path": "http-race.md", "content": "x"},
            headers=_auth(workspace_id),
        )
        results.append((resp.status_code, resp.json()))

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(status for status, _ in results)
    assert statuses == [201, 400]
    error_body = next(body for status, body in results if status == 400)
    assert error_body["detail"] == "Error: File http-race.md already exists"
    assert db_count_memory_entries(db_session, workspace_id) == 1


def test_create_size_cap_enforced(memory_client, workspace_id, monkeypatch):
    import api.routers.internal.memory.route as route_mod

    monkeypatch.setattr(route_mod, "MAX_CONTENT_BYTES", 10)
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": "big.md", "content": "x" * 11},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "maximum size" in resp.json()["detail"]


def test_create_entry_count_cap_enforced(memory_client, workspace_id, monkeypatch):
    import api.routers.internal.memory.route as route_mod

    monkeypatch.setattr(route_mod, "MAX_ENTRIES_PER_WORKSPACE", 1)
    memory_client.post(
        "/internal/memory/create",
        json={"path": "one.md", "content": "x"},
        headers=_auth(workspace_id),
    )
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": "two.md", "content": "x"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "workspace memory store is full" in resp.json()["detail"]


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.md",
        "a/../../escape.md",
        "..\\escape.md",
        "%2e%2e/escape.md",
        "%2e%2e%2fescape.md",
    ],
)
def test_create_path_traversal_rejected(memory_client, workspace_id, bad_path):
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": bad_path, "content": "x"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# str_replace
# ---------------------------------------------------------------------------


def _create(memory_client, workspace_id, path, content):
    resp = memory_client.post(
        "/internal/memory/create",
        json={"path": path, "content": content},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 201
    return resp


def test_str_replace_success(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "line one\nline two\nline three")
    resp = memory_client.post(
        "/internal/memory/str_replace",
        json={"path": "f.md", "old_str": "line two", "new_str": "LINE TWO"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "line one\nLINE TWO\nline three"


def test_concurrent_str_replace_on_same_path_serializes_no_lost_update(app, memory_client):
    """Two concurrent str_replace calls against the same file must not
    silently lose one edit. The locked read (db_get_memory_entry's
    for_update=True) serializes them: whichever commits second re-reads
    the first's already-applied edit, so its own old_str search runs
    against current content instead of a stale copy — it either still
    finds a legitimate (now-relocated) match, or cleanly reports
    "not found" rather than clobbering the first edit.
    """
    from fastapi.testclient import TestClient

    db_plugin = app.database
    assert db_plugin is not None
    web = app.web
    assert web is not None

    with db_plugin.session() as db:
        from api.database import db_create_workspace

        ws = db_create_workspace(db=db, name="race-ws", slug=f"race-ws-{uuid.uuid4().hex[:8]}")
        ws_id = ws.id

    client = TestClient(web.get_asgi_app())
    headers = _auth(ws_id)
    client.post(
        "/internal/memory/create",
        json={"path": "f.md", "content": "alpha"},
        headers=headers,
    )

    results: list[int] = []

    def _replace(old: str, new: str) -> None:
        resp = TestClient(web.get_asgi_app()).post(
            "/internal/memory/str_replace",
            json={"path": "f.md", "old_str": old, "new_str": new},
            headers=headers,
        )
        results.append(resp.status_code)

    threads = [
        threading.Thread(target=_replace, args=("alpha", "beta")),
        threading.Thread(target=_replace, args=("alpha", "gamma")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Whichever str_replace commits second always re-reads content that no
    # longer contains "alpha" (the first edit already replaced it), so
    # exactly one succeeds and the other cleanly reports "not found" — never
    # both succeeding (which would mean one silently overwrote the other)
    # and never a corrupted mix of both edits.
    final = client.get("/internal/memory/view", params={"path": "f.md"}, headers=headers)
    assert final.json()["content"] in ("beta", "gamma")
    assert sorted(results) == [200, 400]


def test_str_replace_not_found(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "hello")
    resp = memory_client.post(
        "/internal/memory/str_replace",
        json={"path": "f.md", "old_str": "missing", "new_str": "x"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "not found" in resp.json()["detail"]


def test_str_replace_multiple_occurrences_reports_line_numbers(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "dup\nother\ndup\nmore\ndup")
    resp = memory_client.post(
        "/internal/memory/str_replace",
        json={"path": "f.md", "old_str": "dup", "new_str": "x"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "1" in detail and "3" in detail and "5" in detail


def test_str_replace_missing_file_404(memory_client, workspace_id):
    resp = memory_client.post(
        "/internal/memory/str_replace",
        json={"path": "nope.md", "old_str": "a", "new_str": "b"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------


def test_insert_at_beginning(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "second")
    resp = memory_client.post(
        "/internal/memory/insert",
        json={"path": "f.md", "insert_line": 0, "text": "first"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "first\nsecond"


def test_insert_out_of_range(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "only line")
    resp = memory_client.post(
        "/internal/memory/insert",
        json={"path": "f.md", "insert_line": 99, "text": "x"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "out of range" in resp.json()["detail"]


def test_insert_missing_file_404(memory_client, workspace_id):
    resp = memory_client.post(
        "/internal/memory/insert",
        json={"path": "nope.md", "insert_line": 0, "text": "x"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_file(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "x")
    resp = memory_client.post(
        "/internal/memory/delete", json={"path": "f.md"}, headers=_auth(workspace_id)
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 1
    assert (
        memory_client.get(
            "/internal/memory/view", params={"path": "f.md"}, headers=_auth(workspace_id)
        ).status_code
        == 404
    )


def test_delete_directory_recursive(memory_client, workspace_id):
    _create(memory_client, workspace_id, "dir/a.md", "x")
    _create(memory_client, workspace_id, "dir/b.md", "y")
    _create(memory_client, workspace_id, "dir/nested/c.md", "z")
    resp = memory_client.post(
        "/internal/memory/delete", json={"path": "dir"}, headers=_auth(workspace_id)
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_count"] == 3


def test_delete_missing_path_404(memory_client, workspace_id):
    resp = memory_client.post(
        "/internal/memory/delete", json={"path": "nope.md"}, headers=_auth(workspace_id)
    )
    assert resp.status_code == 404


def test_delete_root_rejected(memory_client, workspace_id):
    _create(memory_client, workspace_id, "f.md", "x")
    resp = memory_client.post(
        "/internal/memory/delete", json={"path": ""}, headers=_auth(workspace_id)
    )
    assert resp.status_code == 400
    # Nothing was wiped.
    assert (
        memory_client.get(
            "/internal/memory/view", params={"path": "f.md"}, headers=_auth(workspace_id)
        ).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


def test_rename_file(memory_client, workspace_id):
    _create(memory_client, workspace_id, "old.md", "content")
    resp = memory_client.post(
        "/internal/memory/rename",
        json={"old_path": "old.md", "new_path": "new.md"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 200
    assert resp.json()["renamed_count"] == 1
    assert (
        memory_client.get(
            "/internal/memory/view", params={"path": "new.md"}, headers=_auth(workspace_id)
        ).status_code
        == 200
    )
    assert (
        memory_client.get(
            "/internal/memory/view", params={"path": "old.md"}, headers=_auth(workspace_id)
        ).status_code
        == 404
    )


def test_rename_directory(memory_client, workspace_id):
    _create(memory_client, workspace_id, "src/a.md", "x")
    _create(memory_client, workspace_id, "src/b.md", "y")
    resp = memory_client.post(
        "/internal/memory/rename",
        json={"old_path": "src", "new_path": "dst"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 200
    assert resp.json()["renamed_count"] == 2
    assert (
        memory_client.get(
            "/internal/memory/view", params={"path": "dst/a.md"}, headers=_auth(workspace_id)
        ).status_code
        == 200
    )


def test_rename_rejects_ancestor_already_a_file(memory_client, workspace_id):
    _create(memory_client, workspace_id, "notes", "x")
    _create(memory_client, workspace_id, "other.md", "y")
    resp = memory_client.post(
        "/internal/memory/rename",
        json={"old_path": "other.md", "new_path": "notes/other.md"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "ancestor" in resp.json()["detail"]


def test_rename_destination_exists_error(memory_client, workspace_id):
    _create(memory_client, workspace_id, "a.md", "x")
    _create(memory_client, workspace_id, "b.md", "y")
    resp = memory_client.post(
        "/internal/memory/rename",
        json={"old_path": "a.md", "new_path": "b.md"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 400
    assert "already exists" in resp.json()["detail"]


def test_rename_missing_source_404(memory_client, workspace_id):
    resp = memory_client.post(
        "/internal/memory/rename",
        json={"old_path": "nope.md", "new_path": "new.md"},
        headers=_auth(workspace_id),
    )
    assert resp.status_code == 404
