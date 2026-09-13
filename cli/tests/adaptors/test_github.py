"""Tests for GithubAdaptor — URL matching, auth, env, repo resolution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.adaptors.github.adaptor import GithubAdaptor
from src.adaptors.github.client import parse_pr_url


@pytest.fixture
def adaptor() -> GithubAdaptor:
    return GithubAdaptor()


def test_protocol_fields(adaptor: GithubAdaptor) -> None:
    assert adaptor.name == "github"
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
        ("https://github.com/org/repo/pull/123", True),
        ("https://github.com/org/repo/pull/1", True),
        ("http://github.com/foo/bar/pull/9999", True),
        # Issue URLs are now accepted — the respond workflow targets them.
        ("https://github.com/org/repo/issues/123", True),
        ("https://gitlab.com/org/repo/-/merge_requests/123", False),
        ("https://sentry.io/issues/12345", False),
        ("https://github.com/org/repo", False),
        ("not a url", False),
    ],
)
def test_matches(adaptor: GithubAdaptor, url: str, expected: bool) -> None:
    assert adaptor.matches(url) is expected


def test_parse_pr_url() -> None:
    parsed = parse_pr_url("https://github.com/org/repo/pull/42")
    assert parsed == ("org", "repo", 42)


def test_parse_pr_url_invalid() -> None:
    assert parse_pr_url("not a url") is None
    assert parse_pr_url("https://github.com/org/repo") is None


def test_parse_issue_url() -> None:
    from src.adaptors.github.client import parse_issue_url

    assert parse_issue_url("https://github.com/o/r/issues/9") == ("o", "r", 9)
    assert parse_issue_url("https://github.com/o/r/pull/9") is None


def test_parse_comment_url_pr_inline() -> None:
    from src.adaptors.github.client import CommentSurface, parse_comment_url

    parsed = parse_comment_url("https://github.com/o/r/pull/9#discussion_r555")
    assert parsed == ("o", "r", 9, CommentSurface.PR_INLINE_THREAD, 555)


def test_parse_comment_url_pr_top_level() -> None:
    from src.adaptors.github.client import CommentSurface, parse_comment_url

    parsed = parse_comment_url("https://github.com/o/r/pull/9#issuecomment-100")
    assert parsed == ("o", "r", 9, CommentSurface.PR_TOP_LEVEL, 100)


def test_parse_comment_url_review_submission() -> None:
    from src.adaptors.github.client import CommentSurface, parse_comment_url

    parsed = parse_comment_url("https://github.com/o/r/pull/9#pullrequestreview-77")
    assert parsed == ("o", "r", 9, CommentSurface.PR_REVIEW_SUBMISSION, 77)


def test_parse_comment_url_issue() -> None:
    from src.adaptors.github.client import CommentSurface, parse_comment_url

    parsed = parse_comment_url("https://github.com/o/r/issues/42#issuecomment-9")
    assert parsed == ("o", "r", 42, CommentSurface.ISSUE, 9)


def test_parse_comment_url_no_fragment() -> None:
    from src.adaptors.github.client import parse_comment_url

    assert parse_comment_url("https://github.com/o/r/pull/9") is None


def test_select_command_routes_comment_url_to_respond() -> None:
    """A URL fragment flips the auto-detected workflow to respond."""
    a = GithubAdaptor()
    assert a.select_command("https://github.com/o/r/pull/9") == "review"
    assert a.select_command("https://github.com/o/r/pull/9#discussion_r555") == "respond"
    assert a.select_command("https://github.com/o/r/issues/42#issuecomment-9") == "respond"


def test_fetch_respond_context_noop_without_fragment(tmp_path) -> None:
    """No comment fragment in URL → no context files (review/summary mode)."""
    from src.adaptors.github.context import fetch_respond_context

    fetch_respond_context(tmp_path, "https://github.com/o/r/pull/9", "tok")
    assert not (tmp_path / ".context").exists()


def test_fetch_respond_context_top_level_calls_gh_api(tmp_path, monkeypatch) -> None:
    """PR top-level mention: gh api fetches comment + gh pr view fetches PR author."""
    from src.adaptors.github import context

    calls: list[list[str]] = []

    def fake_run(cmd, *, env, timeout=60):
        calls.append(cmd)
        proc = type("P", (), {})()
        proc.returncode = 0
        proc.stderr = ""
        if cmd[:2] == ["gh", "api"]:
            proc.stdout = '{"body": "@jeanclode help", "user": {"login": "bob"}}'
        elif cmd[:3] == ["gh", "pr", "view"]:
            proc.stdout = '{"author": {"login": "alice", "is_bot": false}}'
        else:
            proc.stdout = ""
        return proc

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(tmp_path, "https://github.com/o/r/pull/9#issuecomment-100", "tok")

    ctx = tmp_path / ".context"
    assert (ctx / "platform").read_text().strip() == "github"
    assert (ctx / "repo").read_text().strip() == "o/r"
    assert (ctx / "pr").read_text().strip() == "9"
    assert (ctx / "surface").read_text().strip() == "pr_top_level"
    assert (ctx / "comment_id").read_text().strip() == "100"
    assert (ctx / "mention_body").read_text().strip() == "@jeanclode help"
    assert (ctx / "mention_author").read_text().strip() == "bob"
    assert (ctx / "pr_author").read_text().strip() == "alice"
    assert not (ctx / "is_bot_pr").exists()
    # The `gh api` call must hit the issue-comments endpoint.
    api_call = next(c for c in calls if c[:2] == ["gh", "api"])
    assert "issues/comments/100" in api_call[2]


def test_fetch_respond_context_inline_walks_review_threads(tmp_path, monkeypatch) -> None:
    """Inline-thread mention: walk reviewThreads to find the matching comment by databaseId."""
    from src.adaptors.github import context

    threads_payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "PRT_xyz",
                                "isResolved": False,
                                "path": "src/foo.py",
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRRC_abc",
                                            "databaseId": 555,
                                            "createdAt": "2026-01-01T00:00:00Z",
                                            "bodyText": "@jeanclode look",
                                            "author": {
                                                "__typename": "User",
                                                "login": "carol",
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                }
            }
        }
    }

    def fake_run(cmd, *, env, timeout=60):
        proc = type("P", (), {})()
        proc.returncode = 0
        proc.stderr = ""
        if cmd[:2] == ["gh", "api"] and "graphql" in cmd:
            import json

            proc.stdout = json.dumps(threads_payload)
        elif cmd[:3] == ["gh", "pr", "view"]:
            proc.stdout = '{"author": {"login": "alice", "is_bot": false}}'
        else:
            proc.stdout = ""
        return proc

    monkeypatch.setattr(context, "_run", fake_run)
    context.fetch_respond_context(tmp_path, "https://github.com/o/r/pull/9#discussion_r555", "tok")

    ctx = tmp_path / ".context"
    assert (ctx / "surface").read_text().strip() == "pr_inline_thread"
    assert (ctx / "thread_id").read_text().strip() == "PRT_xyz"
    assert (ctx / "thread_resolved").read_text().strip() == "false"
    assert (ctx / "mention_author").read_text().strip() == "carol"
    assert (ctx / "mention_body").read_text().strip() == "@jeanclode look"


def test_resolve_repo_url(adaptor: GithubAdaptor) -> None:
    url = adaptor.resolve_repo_url("https://github.com/org/repo/pull/123", "tok")
    assert url == "https://github.com/org/repo"


def test_resolve_repo_url_with_override(adaptor: GithubAdaptor) -> None:
    url = adaptor.resolve_repo_url(
        "https://github.com/org/repo/pull/123", "tok", repo_override="other/proj"
    )
    assert url == "https://github.com/other/proj"


def test_resolve_repo_url_with_full_override(adaptor: GithubAdaptor) -> None:
    url = adaptor.resolve_repo_url(
        "https://github.com/org/repo/pull/123",
        "tok",
        repo_override="https://github.com/x/y",
    )
    assert url == "https://github.com/x/y"


def test_build_env(adaptor: GithubAdaptor) -> None:
    env = adaptor.build_env("tok123", ["https://github.com/org/repo/pull/1"])
    assert env == {"GITHUB_TOKEN": "tok123", "GH_TOKEN": "tok123"}


def test_build_prompt_passes_urls_through(adaptor: GithubAdaptor) -> None:
    out = adaptor.build_prompt(["https://github.com/a/b/pull/1", "https://github.com/c/d/pull/2"])
    assert "github.com/a/b/pull/1" in out
    assert "github.com/c/d/pull/2" in out


@patch("src.adaptors.github.auth.os.environ", {"GITHUB_TOKEN": "from-env"})
def test_resolve_auth_from_env(adaptor: GithubAdaptor) -> None:
    assert adaptor.resolve_auth() == "from-env"


@patch("src.adaptors.github.auth.os.environ", {"GH_TOKEN": "from-gh"})
def test_resolve_auth_from_gh_token(adaptor: GithubAdaptor) -> None:
    assert adaptor.resolve_auth() == "from-gh"


@patch("src.adaptors.github.auth.shutil.which", return_value=None)
@patch("src.adaptors.github.auth.os.environ", {})
def test_resolve_auth_returns_none_without_token_or_cli(_which, adaptor: GithubAdaptor) -> None:
    assert adaptor.resolve_auth() is None


@patch("src.adaptors.github.sha.subprocess.run")
def test_fetch_sha_returns_head_sha(mock_run, adaptor: GithubAdaptor) -> None:
    mock_run.return_value.stdout = "abc123def456\n"
    mock_run.return_value.returncode = 0
    sha = adaptor.fetch_sha("https://github.com/org/repo/pull/42", "tok")
    assert sha == "abc123def456"
    cmd = mock_run.call_args.args[0]
    assert "gh" in cmd[0] and "pr" in cmd and "view" in cmd
    assert "42" in cmd
    assert "org/repo" in cmd


@patch("src.adaptors.github.sha.subprocess.run")
def test_fetch_sha_handles_empty_output(mock_run, adaptor: GithubAdaptor) -> None:
    mock_run.return_value.stdout = "\n"
    mock_run.return_value.returncode = 0
    assert adaptor.fetch_sha("https://github.com/org/repo/pull/1", "tok") is None


def test_fetch_sha_invalid_url(adaptor: GithubAdaptor) -> None:
    assert adaptor.fetch_sha("not a url", "tok") is None


@patch("src.adaptors.github.sha.subprocess.run")
def test_fetch_head_ref_returns_branch_name(mock_run, adaptor: GithubAdaptor) -> None:
    mock_run.return_value.stdout = "feat/foo\n"
    mock_run.return_value.returncode = 0
    ref = adaptor.fetch_head_ref("https://github.com/org/repo/pull/42", "tok")
    assert ref == "feat/foo"
    cmd = mock_run.call_args.args[0]
    assert "headRefName" in cmd


def test_fetch_head_ref_invalid_url(adaptor: GithubAdaptor) -> None:
    assert adaptor.fetch_head_ref("not a url", "tok") is None


def test_fetch_context_invalid_url_is_noop(adaptor: GithubAdaptor, tmp_path) -> None:
    adaptor.fetch_context(tmp_path, "not-a-pr-url", "tok")
    assert not (tmp_path / ".context").exists()


@patch("src.adaptors.github.context.subprocess.run")
def test_fetch_context_writes_files(mock_run, adaptor: GithubAdaptor, tmp_path) -> None:
    import json as _json

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "body": "body", "labels": []})
        elif "diff" in cmd:
            result.stdout = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
        elif "graphql" in cmd:
            result.stdout = _json.dumps({"data": {"repository": {"pullRequest": {}}}})
        elif "api" in cmd:
            result.stdout = "[]"
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://github.com/org/repo/pull/1", "tok")

    cache = tmp_path / ".context"
    assert cache.is_dir()
    assert (cache / "platform").read_text().strip() == "github"
    assert (cache / "repo").read_text().strip() == "org/repo"
    assert (cache / "pr").read_text().strip() == "1"
    assert (cache / "pr_description").exists()
    assert (cache / "diff").exists()
    assert (cache / "discussions").exists()


@patch("src.adaptors.github.context.subprocess.run")
def test_fetch_context_renders_threads_and_reviews(
    mock_run, adaptor: GithubAdaptor, tmp_path
) -> None:
    import json as _json

    pr_payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "RT_1",
                                "isResolved": True,
                                "path": "src/x.py",
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRRC_1",
                                            "databaseId": 100,
                                            "createdAt": "2026-04-01T12:00:00Z",
                                            "bodyText": "Looks risky",
                                            "author": {
                                                "__typename": "User",
                                                "login": "alice",
                                            },
                                        },
                                        {
                                            "id": "PRRC_2",
                                            "databaseId": 101,
                                            "createdAt": "2026-04-01T12:30:00Z",
                                            "bodyText": "Replied",
                                            "author": {
                                                "__typename": "Bot",
                                                "login": "jeanclode[bot]",
                                            },
                                        },
                                    ]
                                },
                            },
                            {
                                "id": "RT_2",
                                "isResolved": False,
                                "path": "src/y.py",
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRRC_3",
                                            "databaseId": 102,
                                            "createdAt": "2026-04-02T09:00:00Z",
                                            "bodyText": "Open one",
                                            "author": {
                                                "__typename": "User",
                                                "login": "bob",
                                            },
                                        }
                                    ]
                                },
                            },
                        ]
                    },
                    "reviews": {
                        "nodes": [
                            {
                                "id": "PRR_1",
                                "state": "APPROVED",
                                "bodyText": "LGTM",
                                "submittedAt": "2026-04-01T11:55:00Z",
                                "author": {"__typename": "User", "login": "alice"},
                            }
                        ]
                    },
                    "comments": {
                        "nodes": [
                            {
                                "id": "IC_1",
                                "databaseId": 1,
                                "createdAt": "2026-04-03T10:00:00Z",
                                "bodyText": "Top-level note",
                                "author": {"__typename": "User", "login": "carol"},
                            }
                        ]
                    },
                }
            }
        }
    }

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "body": "", "labels": []})
        elif "diff" in cmd:
            result.stdout = ""
        elif "graphql" in cmd:
            result.stdout = _json.dumps(pr_payload)
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://github.com/org/repo/pull/1", "tok")

    rendered = (tmp_path / ".context" / "discussions").read_text()
    assert "## Review threads" in rendered
    assert "### Thread RT_1 [RESOLVED] on `src/x.py`" in rendered
    assert "### Thread RT_2 [OPEN] on `src/y.py`" in rendered
    assert "**alice** (human, 2026-04-01T12:00:00Z) [PRRC_1]" in rendered
    assert "**jeanclode[bot]** (bot, 2026-04-01T12:30:00Z) [PRRC_2]" in rendered
    assert "## Review submissions" in rendered
    assert "### Review PRR_1 by **alice** (human, 2026-04-01T11:55:00Z) [APPROVED]" in rendered
    assert "LGTM" in rendered
    assert "## Top-level comments" in rendered
    assert "**carol** (human, 2026-04-03T10:00:00Z) [IC_1]: Top-level note" in rendered


@patch("src.adaptors.github.context.subprocess.run")
def test_fetch_context_paginates_review_threads(mock_run, adaptor: GithubAdaptor, tmp_path) -> None:
    import json as _json

    page_one = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR_1"},
                        "nodes": [
                            {
                                "id": f"RT_p1_{i}",
                                "isResolved": False,
                                "path": "f.py",
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": f"PRRC_p1_{i}",
                                            "createdAt": "2026-04-01T00:00:00Z",
                                            "bodyText": f"page-one-{i}",
                                            "author": {
                                                "__typename": "User",
                                                "login": "alice",
                                            },
                                        }
                                    ]
                                },
                            }
                            for i in range(2)
                        ],
                    }
                }
            }
        }
    }
    page_two = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "id": "RT_p2_0",
                                "isResolved": False,
                                "path": "f.py",
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "PRRC_p2_0",
                                            "createdAt": "2026-04-02T00:00:00Z",
                                            "bodyText": "page-two-0",
                                            "author": {"__typename": "User", "login": "alice"},
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                }
            }
        }
    }
    empty_conn = {"data": {"repository": {"pullRequest": {}}}}

    def side_effect(cmd, **_kwargs):
        from unittest.mock import MagicMock

        result = MagicMock()
        result.returncode = 0
        if "view" in cmd:
            result.stdout = _json.dumps({"title": "T", "body": "", "labels": []})
        elif "diff" in cmd:
            result.stdout = ""
        elif "graphql" in cmd:
            joined = " ".join(cmd)
            if "reviewThreads" in joined:
                # Cursor is passed via separate `-f after=CURSOR_1` arg.
                if any("after=CURSOR_1" in part for part in cmd):
                    result.stdout = _json.dumps(page_two)
                else:
                    result.stdout = _json.dumps(page_one)
            else:
                result.stdout = _json.dumps(empty_conn)
        else:
            result.stdout = ""
        return result

    mock_run.side_effect = side_effect
    adaptor.fetch_context(tmp_path, "https://github.com/org/repo/pull/1", "tok")

    rendered = (tmp_path / ".context" / "discussions").read_text()
    assert "RT_p1_0" in rendered
    assert "RT_p1_1" in rendered
    assert "RT_p2_0" in rendered  # second page must be fetched and rendered
