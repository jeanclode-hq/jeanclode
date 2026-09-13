"""Tests for the preflight stage — auth resolution + workspace fetch routing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.cli import CLIArgs, URLInput
from src.runner.preflight import (
    _READ_ONLY_SKILLS,
    SANDBOX_PLACEHOLDER_TOKEN,
    authenticate,
    clone_repos,
    resolve_sources,
)


def _adaptor(*, resolved_token: str | None) -> MagicMock:
    a = MagicMock()
    a.name = "fake"
    a.resolve_auth.return_value = resolved_token
    return a


def test_authenticate_returns_real_token_when_resolved() -> None:
    token = authenticate(_adaptor(resolved_token="real-token"), container_mode=False)
    assert token == "real-token"


def test_authenticate_real_token_takes_precedence_over_container_placeholder() -> None:
    token = authenticate(_adaptor(resolved_token="real-token"), container_mode=True)
    assert token == "real-token"


def test_authenticate_raises_in_local_mode_without_token() -> None:
    with pytest.raises(ValueError, match="No auth token found"):
        authenticate(_adaptor(resolved_token=None), container_mode=False)


def test_authenticate_returns_placeholder_in_container_mode() -> None:
    """In container mode the security-proxy injects real auth on the wire,
    but ``gh``/``glab`` refuse to send a request without a non-empty token —
    so we hand them a placeholder that the proxy strips and replaces."""
    token = authenticate(_adaptor(resolved_token=None), container_mode=True)
    assert token == SANDBOX_PLACEHOLDER_TOKEN
    assert token  # truthy — gh/glab will send the request


# ── Workspace fetch routing (clone vs zipball, by skill) ─────────────


def _src_adaptor(*, sha: str | None, head_ref: str | None = None) -> MagicMock:
    a = MagicMock()
    a.name = "fake"
    a.resolve_repo_url.return_value = "https://github.com/o/r"
    a.fetch_sha.return_value = sha
    a.fetch_head_ref.return_value = head_ref
    return a


def _args(url: str = "https://github.com/o/r/pull/1") -> CLIArgs:
    return CLIArgs(command="run", urls=(URLInput(url=url, repo=None),), adaptor_command=None)


def test_resolve_sources_skips_head_ref_for_read_only_skills() -> None:
    adaptor = _src_adaptor(sha="abc123", head_ref="feat/x")
    sources = resolve_sources(adaptor, _args(), "tok", skill_name="code-review")
    [(_repo, sha, ref)] = list(sources.values())
    assert sha == "abc123"
    assert ref is None
    adaptor.fetch_head_ref.assert_not_called()


def test_resolve_sources_fetches_head_ref_for_write_skills() -> None:
    adaptor = _src_adaptor(sha="abc123", head_ref="feat/x")
    sources = resolve_sources(adaptor, _args(), "tok", skill_name="jeanclode-respond")
    [(_repo, sha, ref)] = list(sources.values())
    assert sha == "abc123"
    assert ref == "feat/x"
    adaptor.fetch_head_ref.assert_called_once()


def test_read_only_skills_set_matches_documented_workflows() -> None:
    """Sanity guard: only review/summary are read-only; respond/fix must clone."""
    assert "code-review" in _READ_ONLY_SKILLS
    assert "pr-summary" in _READ_ONLY_SKILLS
    assert "jeanclode-respond" not in _READ_ONLY_SKILLS
    assert "sentry-fix" not in _READ_ONLY_SKILLS


@patch("src.runner.preflight.clone_repo")
@patch("src.runner.preflight.download_zipball")
def test_clone_repos_uses_zipball_for_read_only_skill(mock_zip, mock_clone, tmp_path: Path) -> None:
    """code-review (read-only) + sha → zipball, not clone."""
    mock_zip.return_value = tmp_path / "workdir"
    mock_zip.return_value.mkdir(parents=True, exist_ok=True)
    sources = {"https://github.com/o/r/pull/1": ("https://github.com/o/r", "abc123", None)}

    clone_repos(sources, _args(), "tok", skill_name="code-review")

    mock_zip.assert_called_once()
    mock_clone.assert_not_called()


@patch("src.runner.preflight.clone_repo")
@patch("src.runner.preflight.download_zipball")
def test_clone_repos_uses_git_clone_for_write_skill_with_sha(
    mock_zip, mock_clone, tmp_path: Path
) -> None:
    """jeanclode-respond + sha + head_ref → git clone --branch <ref>, never zipball."""
    mock_clone.return_value = tmp_path / "workdir"
    mock_clone.return_value.mkdir(parents=True, exist_ok=True)
    sources = {
        "https://github.com/o/r/pull/1": ("https://github.com/o/r", "abc123", "feat/foo"),
    }

    clone_repos(sources, _args(), "tok", skill_name="jeanclode-respond")

    mock_zip.assert_not_called()
    mock_clone.assert_called_once()
    # head_ref is forwarded so the working tree lands on the PR branch
    assert mock_clone.call_args.kwargs.get("ref") == "feat/foo"


@patch("src.runner.preflight.clone_repo")
@patch("src.runner.preflight.download_zipball")
def test_clone_repos_clones_default_branch_when_no_sha(
    mock_zip, mock_clone, tmp_path: Path
) -> None:
    """sentry-fix path: no sha → clone default branch (no ref), no zipball."""
    mock_clone.return_value = tmp_path / "workdir"
    mock_clone.return_value.mkdir(parents=True, exist_ok=True)
    sources = {"https://sentry.io/issues/1": ("https://github.com/o/r", None, None)}

    clone_repos(sources, _args("https://sentry.io/issues/1"), "tok", skill_name="sentry-fix")

    mock_zip.assert_not_called()
    mock_clone.assert_called_once()
    assert mock_clone.call_args.kwargs.get("ref") is None


# ── Related-repo cloning (repo-group workspace) ──────────────────────


def _args_with_related(*related: str, url: str = "https://sentry.io/issues/1") -> CLIArgs:
    return CLIArgs(
        command="run",
        urls=(URLInput(url=url, repo=None, related_repos=tuple(related)),),
        adaptor_command=None,
    )


@patch("src.runner.preflight.clone_repo")
@patch("src.runner.preflight.download_zipball")
def test_clone_repos_clones_related_repos_alongside_primary(
    mock_zip, mock_clone, tmp_path: Path
) -> None:
    """--related-repo URLs get cloned into the same workspace as the primary."""
    primary_dir = tmp_path / "primary"
    related_dir = tmp_path / "related"
    mock_clone.side_effect = [primary_dir, related_dir]
    sources = {"https://sentry.io/issues/1": ("https://github.com/o/r", None, None)}
    args = _args_with_related("https://github.com/org/shared-lib")

    _, cwd, related = clone_repos(sources, args, "tok", skill_name="sentry-fix")

    assert mock_clone.call_count == 2
    assert cwd == str(primary_dir)
    assert related == [("https://github.com/org/shared-lib", str(related_dir))]


@patch("src.runner.preflight.clone_repo")
@patch("src.runner.preflight.download_zipball")
def test_clone_repos_skips_related_repo_already_fetched_as_primary(
    mock_zip, mock_clone, tmp_path: Path
) -> None:
    """A related repo that's identical to the primary isn't cloned twice."""
    primary_dir = tmp_path / "primary"
    mock_clone.return_value = primary_dir
    sources = {"https://sentry.io/issues/1": ("https://github.com/o/r", None, None)}
    args = _args_with_related("https://github.com/o/r")

    _, _cwd, related = clone_repos(sources, args, "tok", skill_name="sentry-fix")

    mock_clone.assert_called_once()
    assert related == []


@patch("src.runner.preflight.clone_repo")
@patch("src.runner.preflight.download_zipball")
def test_clone_repos_related_repo_failure_is_non_fatal(
    mock_zip, mock_clone, tmp_path: Path
) -> None:
    """A related repo that fails to clone is skipped, not a hard failure."""
    primary_dir = tmp_path / "primary"
    mock_clone.side_effect = [primary_dir, RuntimeError("clone failed")]
    sources = {"https://sentry.io/issues/1": ("https://github.com/o/r", None, None)}
    args = _args_with_related("https://github.com/org/broken-repo")

    _, cwd, related = clone_repos(sources, args, "tok", skill_name="sentry-fix")

    assert cwd == str(primary_dir)
    assert related == []
