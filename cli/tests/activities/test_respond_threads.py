"""``count_unresolved_threads`` — the deterministic converged-loop signal."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from src.activities.respond import count_unresolved_threads
from src.runtime.bus import EventBus
from src.runtime.context import RunContext


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(cwd=tmp_path, workspace=tmp_path, events=EventBus())


def _completed(rc: int = 0, *, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _gh_page(*resolved_flags: bool, has_next: bool = False, cursor: str = "") -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                            "nodes": [{"isResolved": r} for r in resolved_flags],
                        }
                    }
                }
            }
        }
    )


def test_github_counts_only_unresolved(tmp_path: Path) -> None:
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.return_value = _completed(0, stdout=_gh_page(True, False, False))
        assert count_unresolved_threads("github", "acme/app", "7", ctx=_ctx(tmp_path)) == 2


def test_github_all_resolved_is_zero(tmp_path: Path) -> None:
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.return_value = _completed(0, stdout=_gh_page(True, True))
        assert count_unresolved_threads("github", "acme/app", "7", ctx=_ctx(tmp_path)) == 0


def test_github_paginates(tmp_path: Path) -> None:
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.side_effect = [
            _completed(0, stdout=_gh_page(False, has_next=True, cursor="c1")),
            _completed(0, stdout=_gh_page(False, True)),
        ]
        assert count_unresolved_threads("github", "acme/app", "7", ctx=_ctx(tmp_path)) == 2
    assert run.call_count == 2


def test_failure_is_none_not_zero(tmp_path: Path) -> None:
    """None and 0 must stay distinguishable — a caller reading an API blip as
    "everything is resolved" would ping on an MR that is still mid-loop."""
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.return_value = _completed(1, stderr="rate limited")
        assert count_unresolved_threads("github", "acme/app", "7", ctx=_ctx(tmp_path)) is None


def test_unparseable_output_is_none(tmp_path: Path) -> None:
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.return_value = _completed(0, stdout="<html>")
        assert count_unresolved_threads("github", "acme/app", "7", ctx=_ctx(tmp_path)) is None


def test_subprocess_error_is_none(tmp_path: Path) -> None:
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.side_effect = OSError("gh missing")
        assert count_unresolved_threads("github", "acme/app", "7", ctx=_ctx(tmp_path)) is None


def test_missing_repo_or_pr_is_none(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert count_unresolved_threads("github", "acme/app", "", ctx=ctx) is None
    assert count_unresolved_threads("github", "", "7", ctx=ctx) is None


def test_gitlab_counts_resolvable_and_open_only(tmp_path: Path) -> None:
    """A plain comment carries neither flag and isn't a thread anyone can
    close, so it must not hold the loop open forever."""
    discussions = json.dumps(
        [
            {"notes": [{"resolvable": True, "resolved": False}]},
            {"notes": [{"resolvable": True, "resolved": True}]},
            {"notes": [{"body": "just a comment"}]},
        ]
    )
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.return_value = _completed(0, stdout=discussions)
        assert count_unresolved_threads("gitlab", "group/app", "42", ctx=_ctx(tmp_path)) == 1


def test_gitlab_uses_the_mr_host(tmp_path: Path) -> None:
    with patch("src.activities.respond.threads.subprocess.run") as run:
        run.return_value = _completed(0, stdout="[]")
        count_unresolved_threads(
            "gitlab",
            "group/app",
            "42",
            target_url="https://gitlab.example.com/group/app/-/merge_requests/42",
            ctx=_ctx(tmp_path),
        )
    assert run.call_args.kwargs["env"]["GITLAB_HOST"] == "https://gitlab.example.com"
