"""Tests for GitlabAdaptor — URL matching, auth, env, host detection."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.adaptors.gitlab.adaptor import GitlabAdaptor
from src.adaptors.gitlab.client import parse_mr_url


@pytest.fixture
def adaptor() -> GitlabAdaptor:
    return GitlabAdaptor()


def test_protocol_fields(adaptor: GitlabAdaptor) -> None:
    assert adaptor.name == "gitlab"
    assert adaptor.default_command == "review"
    assert adaptor.skills == {
        "review": "code-review",
        "summary": "pr-summary",
        "respond": "jeanclode-respond",
        "issue-resolve": "issue-resolve",
    }


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://gitlab.com/group/project/-/merge_requests/42", True),
        ("https://gitlab.example.com/group/project/-/merge_requests/1", True),
        ("https://gitlab.com/g/sub/proj/-/merge_requests/9", True),
        # Issue URLs are accepted for the respond workflow.
        ("https://gitlab.com/group/project/-/issues/42", True),
        # Work item URLs (GitLab 15+ UI format) must also match.
        ("https://gitlab.example.com/g/p/-/work_items/30", True),
        ("https://github.com/org/repo/pull/123", False),
        ("https://other.com/group/project/-/merge_requests/1", False),
    ],
)
def test_matches(adaptor: GitlabAdaptor, url: str, expected: bool) -> None:
    assert adaptor.matches(url) is expected


def test_parse_issue_url() -> None:
    from src.adaptors.gitlab.client import parse_issue_url

    parsed = parse_issue_url("https://gitlab.com/g/p/-/issues/9")
    assert parsed == ("gitlab.com", "g/p", 9)
    assert parse_issue_url("https://gitlab.com/g/p/-/merge_requests/9") is None


def test_parse_issue_url_work_items() -> None:
    from src.adaptors.gitlab.client import parse_issue_url

    parsed = parse_issue_url("https://gitlab.example.com/g/p/-/work_items/30")
    assert parsed == ("gitlab.example.com", "g/p", 30)


def test_parse_note_url_work_item() -> None:
    from src.adaptors.gitlab.client import parse_note_url

    parsed = parse_note_url("https://gitlab.example.com/g/p/-/work_items/30#note_85833")
    assert parsed == ("gitlab.example.com", "g/p", 30, "issue", 85833)


def test_parse_note_url_mr() -> None:
    from src.adaptors.gitlab.client import parse_note_url

    parsed = parse_note_url("https://gitlab.com/g/p/-/merge_requests/1#note_500")
    assert parsed == ("gitlab.com", "g/p", 1, "merge_request", 500)


def test_parse_note_url_issue() -> None:
    from src.adaptors.gitlab.client import parse_note_url

    parsed = parse_note_url("https://gitlab.com/g/p/-/issues/3#note_777")
    assert parsed == ("gitlab.com", "g/p", 3, "issue", 777)


def test_select_command_routes_note_url_to_respond() -> None:
    a = GitlabAdaptor()
    assert a.select_command("https://gitlab.com/g/p/-/merge_requests/1") == "review"
    assert a.select_command("https://gitlab.com/g/p/-/merge_requests/1#note_500") == "respond"
    assert a.select_command("https://gitlab.com/g/p/-/issues/3#note_777") == "respond"
    assert (
        a.select_command("https://gitlab.example.com/g/p/-/work_items/30#note_85833") == "respond"
    )


def test_fetch_respond_context_noop_without_fragment(tmp_path) -> None:
    from src.adaptors.gitlab.context import fetch_respond_context

    fetch_respond_context(tmp_path, "https://gitlab.com/g/p/-/merge_requests/1", "tok")
    assert not (tmp_path / ".context").exists()


def _respond_run(discussions: str, *, note: str = "") -> Any:
    """Build a ``_run`` double for the respond-context fetch.

    ``discussions`` is the JSON body the Discussions API returns —
    the only endpoint that exposes ``discussion_id``. ``note`` is the
    flat single-note fallback, used only when the discussion scan finds
    nothing (GitLab never puts ``discussion_id`` there).
    """

    def fake_run(cmd, *, env, timeout=60):
        proc = type("P", (), {})()
        proc.returncode = 0
        proc.stderr = ""
        if cmd[:2] == ["glab", "api"] and "/discussions" in cmd[-1]:
            # Single page — the paginator stops on a short chunk.
            proc.stdout = discussions if cmd[-1].endswith("&page=1") else "[]"
        elif cmd[:2] == ["glab", "api"] and "/notes/" in cmd[-1]:
            proc.stdout = note
        elif cmd[:3] == ["glab", "mr", "view"]:
            proc.stdout = '{"author": {"username": "alice", "bot": false}}'
        else:
            proc.stdout = ""
        return proc

    return fake_run


def test_fetch_respond_context_mr_inline(tmp_path, monkeypatch) -> None:
    """MR note with ``position`` set → pr_inline_thread surface."""
    from src.adaptors.gitlab import context

    fake_run = _respond_run(
        '[{"id": "disc-abc", "individual_note": false, "notes": ['
        '{"id": 500, "body": "@jeanclode look", "author": {"username": "carol"},'
        ' "position": {"new_path": "src/foo.py", "new_line": 10}}]}]'
    )

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(
        tmp_path, "https://gitlab.com/g/p/-/merge_requests/1#note_500", "tok"
    )

    ctx = tmp_path / ".context"
    assert (ctx / "platform").read_text().strip() == "gitlab"
    assert (ctx / "repo").read_text().strip() == "g/p"
    assert (ctx / "pr").read_text().strip() == "1"
    assert (ctx / "surface").read_text().strip() == "pr_inline_thread"
    assert (ctx / "comment_id").read_text().strip() == "500"
    assert (ctx / "thread_id").read_text().strip() == "disc-abc"
    assert (ctx / "mention_author").read_text().strip() == "carol"
    assert (ctx / "mention_body").read_text().strip() == "@jeanclode look"


def test_fetch_respond_context_mr_top_level(tmp_path, monkeypatch) -> None:
    """MR note without ``position`` → pr_top_level surface."""
    from src.adaptors.gitlab import context

    fake_run = _respond_run(
        '[{"id": "d2", "individual_note": true, "notes": ['
        '{"id": 600, "body": "@jeanclode hi", "author": {"username": "dave"}}]}]'
    )

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(
        tmp_path, "https://gitlab.com/g/p/-/merge_requests/1#note_600", "tok"
    )
    ctx = tmp_path / ".context"
    assert (ctx / "surface").read_text().strip() == "pr_top_level"
    assert (ctx / "thread_id").read_text().strip() == "d2"


def test_fetch_respond_context_issue_resolves_discussion_id(tmp_path, monkeypatch) -> None:
    """The regression: a plain issue comment still has to yield a thread id.

    GitLab's flat ``.../notes/:id`` endpoint never returns
    ``discussion_id``, so the id has to come off the Discussions API —
    including for an ``individual_note`` discussion, which is what a
    standalone top-level comment produces.
    """
    from src.adaptors.gitlab import context

    fake_run = _respond_run(
        '[{"id": "sys-1", "individual_note": true, "notes": ['
        '{"id": 999, "system": true, "body": "added label"}]},'
        '{"id": "6a9c1750b37d513a43987b574953fceb50b03ce7", "individual_note": true,'
        ' "notes": [{"id": 103337, "body": "@jeanclode-bot how are you",'
        ' "author": {"username": "adsa"}}]}]'
    )

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(
        tmp_path,
        "https://gitlab.example.dev/jdoe/webshop/-/work_items/10#note_103337",
        "tok",
    )

    ctx = tmp_path / ".context"
    assert (ctx / "issue").read_text().strip() == "10"
    assert (ctx / "surface").read_text().strip() == "issue"
    assert (ctx / "comment_id").read_text().strip() == "103337"
    assert (ctx / "thread_id").read_text().strip() == "6a9c1750b37d513a43987b574953fceb50b03ce7"
    assert (ctx / "mention_author").read_text().strip() == "adsa"
    assert (ctx / "mention_body").read_text().strip() == "@jeanclode-bot how are you"


def test_fetch_respond_context_falls_back_to_flat_note(tmp_path, monkeypatch) -> None:
    """Discussions unavailable → still populate the body, with an empty thread id."""
    from src.adaptors.gitlab import context

    fake_run = _respond_run(
        "[]",
        note='{"id": 700, "body": "@jeanclode-bot ping", "author": {"username": "erin"}}',
    )

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(tmp_path, "https://gitlab.com/g/p/-/issues/4#note_700", "tok")

    ctx = tmp_path / ".context"
    assert (ctx / "thread_id").read_text().strip() == ""
    assert (ctx / "mention_author").read_text().strip() == "erin"
    assert (ctx / "mention_body").read_text().strip() == "@jeanclode-bot ping"


def test_fetch_respond_context_paginates_to_find_note(tmp_path, monkeypatch) -> None:
    """A note on page 2 of the discussions list is still resolved."""
    from src.adaptors.gitlab import context

    page1 = [
        {"id": f"d{i}", "individual_note": True, "notes": [{"id": i, "body": "x"}]}
        for i in range(100)
    ]
    page2 = [
        {
            "id": "target-disc",
            "individual_note": True,
            "notes": [{"id": 4242, "body": "@jeanclode-bot hi", "author": {"username": "zoe"}}],
        }
    ]

    def fake_run(cmd, *, env, timeout=60):
        proc = type("P", (), {})()
        proc.returncode = 0
        proc.stderr = ""
        proc.stdout = json.dumps(page1 if cmd[-1].endswith("&page=1") else page2)
        return proc

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(tmp_path, "https://gitlab.com/g/p/-/issues/4#note_4242", "tok")

    ctx = tmp_path / ".context"
    assert (ctx / "thread_id").read_text().strip() == "target-disc"
    assert (ctx / "mention_author").read_text().strip() == "zoe"


def test_parse_mr_url_simple() -> None:
    parsed = parse_mr_url("https://gitlab.com/group/project/-/merge_requests/42")
    assert parsed == ("gitlab.com", "group/project", 42)


def test_parse_mr_url_nested_groups() -> None:
    parsed = parse_mr_url("https://gitlab.com/g/sub/proj/-/merge_requests/9")
    assert parsed == ("gitlab.com", "g/sub/proj", 9)


def test_parse_mr_url_self_hosted() -> None:
    parsed = parse_mr_url("https://gitlab.example.com/group/project/-/merge_requests/1")
    assert parsed == ("gitlab.example.com", "group/project", 1)


def test_resolve_repo_url(adaptor: GitlabAdaptor) -> None:
    url = adaptor.resolve_repo_url("https://gitlab.com/group/project/-/merge_requests/42", "tok")
    assert url == "https://gitlab.com/group/project"


def test_resolve_repo_url_self_hosted(adaptor: GitlabAdaptor) -> None:
    url = adaptor.resolve_repo_url(
        "https://gitlab.example.com/group/project/-/merge_requests/1", "tok"
    )
    assert url == "https://gitlab.example.com/group/project"


def test_build_env_gitlab_com(adaptor: GitlabAdaptor) -> None:
    env = adaptor.build_env("tok", ["https://gitlab.com/g/p/-/merge_requests/1"])
    assert env == {"GITLAB_TOKEN": "tok"}


def test_build_env_self_hosted_sets_host(adaptor: GitlabAdaptor) -> None:
    env = adaptor.build_env("tok", ["https://gitlab.example.com/g/p/-/merge_requests/1"])
    assert env == {"GITLAB_TOKEN": "tok", "GITLAB_HOST": "https://gitlab.example.com"}


@patch("src.adaptors.gitlab.auth.os.environ", {"GITLAB_TOKEN": "tok-env"})
def test_resolve_auth_from_env(adaptor: GitlabAdaptor) -> None:
    assert adaptor.resolve_auth() == "tok-env"


@patch("src.adaptors.gitlab.auth.os.environ", {"GITLAB_ACCESS_TOKEN": "tok-glab-env"})
def test_resolve_auth_from_gitlab_access_token(adaptor: GitlabAdaptor) -> None:
    assert adaptor.resolve_auth() == "tok-glab-env"


@patch("src.adaptors.gitlab.auth.subprocess.run")
@patch("src.adaptors.gitlab.auth.shutil.which", return_value="/usr/bin/glab")
@patch("src.adaptors.gitlab.auth.os.environ", {})
def test_resolve_auth_from_glab_status(_which, run, adaptor: GitlabAdaptor) -> None:
    run.return_value = SimpleNamespace(
        stdout="gitlab.example.org\n  ✓ Token found: glpat-from-keyring\n",
        stderr="",
    )
    assert adaptor.resolve_auth() == "glpat-from-keyring"


@patch("src.adaptors.gitlab.auth.subprocess.run")
@patch("src.adaptors.gitlab.auth.shutil.which", return_value="/usr/bin/glab")
@patch("src.adaptors.gitlab.auth.os.environ", {})
def test_resolve_auth_ignores_masked_token(_which, run, adaptor: GitlabAdaptor) -> None:
    run.return_value = SimpleNamespace(stdout="  ✓ Token: ****************\n", stderr="")
    assert adaptor.resolve_auth() is None


@patch("src.adaptors.gitlab.auth.shutil.which", return_value=None)
@patch("src.adaptors.gitlab.auth.os.environ", {})
def test_resolve_auth_returns_none(_which, adaptor: GitlabAdaptor) -> None:
    assert adaptor.resolve_auth() is None


@patch("src.adaptors.gitlab.sha.subprocess.run")
def test_fetch_sha_returns_head_sha(mock_run, adaptor: GitlabAdaptor) -> None:
    import json as _json

    mock_run.return_value.stdout = _json.dumps({"sha": "deadbeef1234"})
    mock_run.return_value.returncode = 0
    sha = adaptor.fetch_sha("https://gitlab.com/group/proj/-/merge_requests/42", "tok")
    assert sha == "deadbeef1234"


@patch("src.adaptors.gitlab.sha.subprocess.run")
def test_fetch_sha_falls_back_to_diff_refs(mock_run, adaptor: GitlabAdaptor) -> None:
    import json as _json

    mock_run.return_value.stdout = _json.dumps({"diff_refs": {"head_sha": "diffsha"}})
    mock_run.return_value.returncode = 0
    sha = adaptor.fetch_sha("https://gitlab.com/group/proj/-/merge_requests/42", "tok")
    assert sha == "diffsha"


def test_fetch_sha_invalid_url(adaptor: GitlabAdaptor) -> None:
    assert adaptor.fetch_sha("not a url", "tok") is None


@patch("src.adaptors.gitlab.sha.subprocess.run")
def test_fetch_head_ref_returns_source_branch(mock_run, adaptor: GitlabAdaptor) -> None:
    import json as _json

    mock_run.return_value.stdout = _json.dumps({"source_branch": "topic/x"})
    mock_run.return_value.returncode = 0
    ref = adaptor.fetch_head_ref("https://gitlab.com/group/proj/-/merge_requests/42", "tok")
    assert ref == "topic/x"


def test_fetch_head_ref_invalid_url(adaptor: GitlabAdaptor) -> None:
    assert adaptor.fetch_head_ref("not a url", "tok") is None


def test_fetch_context_invalid_url_is_noop(adaptor: GitlabAdaptor, tmp_path) -> None:
    adaptor.fetch_context(tmp_path, "not-a-mr-url", "tok")
    assert not (tmp_path / ".context").exists()


@patch("src.adaptors.gitlab.context.subprocess.run")
def test_fetch_context_writes_files(mock_run, adaptor: GitlabAdaptor, tmp_path) -> None:
    import json as _json

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "description": "body", "labels": []})
        elif "diff" in cmd:
            result.stdout = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        elif "api" in cmd:
            result.stdout = "[]"
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://gitlab.com/group/proj/-/merge_requests/42", "tok")

    cache = tmp_path / ".context"
    assert cache.is_dir()
    assert (cache / "platform").read_text().strip() == "gitlab"
    assert (cache / "repo").read_text().strip() == "group/proj"
    assert (cache / "pr").read_text().strip() == "42"
    assert (cache / "diff").read_text() != ""


@patch("src.adaptors.gitlab.context.subprocess.run")
def test_fetch_context_renders_threads_and_approvals(
    mock_run, adaptor: GitlabAdaptor, tmp_path
) -> None:
    import json as _json

    discussions_payload = [
        {
            "id": "disc_resolved",
            "individual_note": False,
            "notes": [
                {
                    "id": 1001,
                    "body": "Risky bit",
                    "created_at": "2026-04-01T12:00:00Z",
                    "system": False,
                    "resolvable": True,
                    "resolved": True,
                    "author": {"username": "alice", "bot": False},
                    "position": {"new_path": "src/x.py"},
                },
                {
                    "id": 1002,
                    "body": "Replied",
                    "created_at": "2026-04-01T12:30:00Z",
                    "system": False,
                    "resolvable": True,
                    "resolved": True,
                    "author": {"username": "jeanclode-bot", "bot": True},
                    "position": {"new_path": "src/x.py"},
                },
            ],
        },
        {
            "id": "disc_open",
            "individual_note": False,
            "notes": [
                {
                    "id": 1003,
                    "body": "Open thread",
                    "created_at": "2026-04-02T09:00:00Z",
                    "system": False,
                    "resolvable": True,
                    "resolved": False,
                    "author": {"username": "bob", "bot": False},
                    "position": {"new_path": "src/y.py"},
                }
            ],
        },
        {
            "id": "disc_top",
            "individual_note": True,
            "notes": [
                {
                    "id": 1004,
                    "body": "Top-level note",
                    "created_at": "2026-04-03T10:00:00Z",
                    "system": False,
                    "author": {"username": "carol", "bot": False},
                }
            ],
        },
        {
            "id": "disc_system",
            "individual_note": True,
            "notes": [
                {
                    "id": 1005,
                    "body": "assigned to @x",
                    "system": True,
                    "author": {"username": "ghost"},
                }
            ],
        },
    ]
    approvals_payload = {
        "approved_by": [
            {
                "user": {"id": 7, "username": "alice", "bot": False},
                "created_at": "2026-04-01T11:55:00Z",
            }
        ]
    }

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "description": "", "labels": []})
        elif "diff" in cmd:
            result.stdout = ""
        elif "api" in cmd:
            joined = " ".join(cmd)
            if "discussions" in joined:
                result.stdout = _json.dumps(discussions_payload)
            elif "approvals" in joined:
                result.stdout = _json.dumps(approvals_payload)
            else:
                result.stdout = "[]"
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://gitlab.com/group/proj/-/merge_requests/42", "tok")

    rendered = (tmp_path / ".context" / "discussions").read_text()
    assert "### Thread disc_resolved [RESOLVED] on `src/x.py`" in rendered
    assert "### Thread disc_open [OPEN] on `src/y.py`" in rendered
    assert "**alice** (human, 2026-04-01T12:00:00Z) [1001]" in rendered
    assert "**jeanclode-bot** (bot, 2026-04-01T12:30:00Z) [1002]" in rendered
    assert "**carol** (human, 2026-04-03T10:00:00Z) [1004]: Top-level note" in rendered
    assert "[APPROVED]" in rendered
    assert "**alice**" in rendered
    assert "assigned to" not in rendered


@patch("src.adaptors.gitlab.context.subprocess.run")
def test_fetch_context_paginates_discussions(mock_run, adaptor: GitlabAdaptor, tmp_path) -> None:
    import json as _json

    def make_page(start: int, count: int) -> list[dict]:
        return [
            {
                "id": f"disc_{i}",
                "individual_note": True,
                "notes": [
                    {
                        "id": i,
                        "body": f"note-{i}",
                        "created_at": "2026-04-01T00:00:00Z",
                        "system": False,
                        "author": {"username": "alice", "bot": False},
                    }
                ],
            }
            for i in range(start, start + count)
        ]

    page_one = make_page(1, 100)  # full page → triggers next-page fetch
    page_two = make_page(101, 3)  # short page → stops pagination

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "description": "", "labels": []})
        elif "diff" in cmd:
            result.stdout = ""
        elif "api" in cmd:
            endpoint = next((p for p in cmd if "merge_requests" in p), "")
            if "discussions" in endpoint:
                if "page=2" in endpoint:
                    result.stdout = _json.dumps(page_two)
                else:
                    result.stdout = _json.dumps(page_one)
            else:
                result.stdout = "{}"
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://gitlab.com/group/proj/-/merge_requests/42", "tok")

    rendered = (tmp_path / ".context" / "discussions").read_text()
    assert "[1]: note-1" in rendered  # first page
    assert "[103]: note-103" in rendered  # second page must be fetched


@patch("src.adaptors.gitlab.context.subprocess.run")
def test_fetch_context_handles_null_approver_user(
    mock_run, adaptor: GitlabAdaptor, tmp_path
) -> None:
    import json as _json

    approvals_payload = {
        "approved_by": [
            {"user": None, "created_at": "2026-04-01T11:55:00Z"},
            {"user": None, "created_at": "2026-04-02T11:55:00Z"},
            {"user": {"id": 7, "username": "alice", "bot": False}},
        ]
    }

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "description": "", "labels": []})
        elif "diff" in cmd:
            result.stdout = ""
        elif "api" in cmd:
            endpoint = next((p for p in cmd if "merge_requests" in p), "")
            if "approvals" in endpoint:
                result.stdout = _json.dumps(approvals_payload)
            else:
                result.stdout = "[]"
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://gitlab.com/group/proj/-/merge_requests/42", "tok")

    rendered = (tmp_path / ".context" / "discussions").read_text()
    assert "**ghost**" in rendered  # null user rendered as ghost, no AttributeError
    assert "**alice**" in rendered
    # Multiple null-user approvers must get distinct IDs (not all "ghost").
    assert "Review ghost-0" in rendered
    assert "Review ghost-1" in rendered
