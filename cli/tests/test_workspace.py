"""Tests for workspace creation and cleanup."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src.workspace import (
    _CLONE_MAX_ATTEMPTS,
    _embed_token,
    cleanup_workspace,
    clone_repo,
    create_workspace,
    disable_commit_signing,
)


def _fake_run(clone_results: list[int]):
    """subprocess.run stand-in: pops ``clone_results`` for each ``git clone``
    call (in order), always succeeds for any other git command (the
    post-clone identity/signing setup)."""

    def run(cmd, **kwargs):
        returncode = clone_results.pop(0) if "clone" in cmd else 0
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    return run


def test_create_workspace() -> None:
    ws = create_workspace()
    assert ws.exists()
    assert ws.name.startswith("jeanclode-")
    ws.rmdir()


def test_cleanup_workspace(tmp_path) -> None:
    ws = tmp_path / "jeanclode-test"
    ws.mkdir()
    (ws / "file.txt").write_text("hello")

    cleanup_workspace(ws)
    assert not ws.exists()


def test_cleanup_workspace_missing(tmp_path) -> None:
    ws = tmp_path / "jeanclode-missing"
    # Should not raise
    cleanup_workspace(ws)


@pytest.mark.parametrize(
    ("url", "token", "expected"),
    [
        (
            "https://github.com/owner/repo",
            "ghp_real",
            "https://x-access-token:ghp_real@github.com/owner/repo",
        ),
        (
            "https://gitlab.com/group/project",
            "glpat_real",
            "https://oauth2:glpat_real@gitlab.com/group/project",
        ),
        (
            "https://gitlab.example.com/group/project",
            "glpat_real",
            "https://oauth2:glpat_real@gitlab.example.com/group/project",
        ),
    ],
)
def test_embed_token_injects_auth_for_known_hosts(url: str, token: str, expected: str) -> None:
    assert _embed_token(url, token) == expected


@pytest.mark.parametrize(
    ("url", "token"),
    [
        # Empty token — local mode without auth, no-op
        ("https://github.com/owner/repo", ""),
        # Sandbox placeholder — proxy handles injection on the wire
        ("https://github.com/owner/repo", "sandbox-placeholder"),
        # The backend's own placeholders count too: GITLAB_TOKEN=proxy-injected
        # exists only so glab makes the request, and embedding it sends the
        # literal string as the git password.
        ("https://gitlab.example.com/group/repo", "proxy-injected"),
        ("https://github.com/owner/repo", "sandbox-proxy-injected"),
        # Non-HTTPS — no point embedding
        ("git@github.com:owner/repo.git", "ghp_real"),
        # Unknown host — don't blindly embed
        ("https://example.com/owner/repo", "ghp_real"),
    ],
)
def test_embed_token_noop_cases(url: str, token: str) -> None:
    assert _embed_token(url, token) == url


def test_disable_commit_signing_overrides_signing_host_config(tmp_path) -> None:
    """A host with commit.gpgsign=true and a broken signing program must not
    block commits in our clone — this is what a real dev machine looks like
    (see cli/CLAUDE.md / QA notes: interactive pinentry fails non-interactively).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "x@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "true"], cwd=repo, check=True)
    # Any gpg.program that exits non-zero simulates the interactive pinentry
    # failure without actually needing gpg or a key set up in this test.
    subprocess.run(["git", "config", "gpg.program", "false"], cwd=repo, check=True)

    disable_commit_signing(repo)

    gpgsign = subprocess.run(
        ["git", "config", "--get", "commit.gpgsign"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert gpgsign == "false"

    # The real assertion: a commit that would have hung/failed on signing
    # now succeeds.
    commit = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "test"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert commit.returncode == 0, commit.stderr


def test_clone_repo_retries_transient_failure_then_succeeds(tmp_path) -> None:
    """A clone that fails because the proxy sidecar died mid-stream (see
    security-proxy) shouldn't permanently drop the repo for the run — it
    should recover once the sidecar is back."""
    results = [128, 128, 0]  # two transient failures, then success
    with (
        patch("src.workspace.subprocess.run", side_effect=_fake_run(results)) as run,
        patch("src.workspace.time.sleep") as sleep,
    ):
        target = clone_repo("https://github.com/owner/repo", tmp_path)

    assert target == tmp_path / "owner_repo"
    clone_calls = [c for c in run.call_args_list if "clone" in c.args[0]]
    assert len(clone_calls) == 3
    # Backoff doubles each retry: 500ms, then 1000ms — only 2 sleeps for 3 attempts.
    assert [c.args[0] for c in sleep.call_args_list] == [0.5, 1.0]


def test_clone_repo_raises_after_exhausting_retries(tmp_path) -> None:
    results = [128] * _CLONE_MAX_ATTEMPTS
    with (
        patch("src.workspace.subprocess.run", side_effect=_fake_run(results)) as run,
        patch("src.workspace.time.sleep"),
        pytest.raises(RuntimeError, match="clone failed"),
    ):
        clone_repo("https://github.com/owner/repo", tmp_path)

    clone_calls = [c for c in run.call_args_list if "clone" in c.args[0]]
    assert len(clone_calls) == _CLONE_MAX_ATTEMPTS
