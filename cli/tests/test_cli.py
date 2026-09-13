"""Tests for CLI argument parsing."""

from __future__ import annotations

import pytest

from src.cli import CLIArgs, URLInput, parse_args

# -- Single URL --


def test_single_url() -> None:
    args = parse_args(["https://sentry.io/issues/12345"])
    assert args.command == "run"
    assert args.urls == (URLInput(url="https://sentry.io/issues/12345"),)
    assert args.adaptor_command is None
    assert args.dry_run is False
    assert args.debug is False


def test_dry_run_flag() -> None:
    args = parse_args(["https://sentry.io/issues/12345", "--dry-run"])
    assert args.dry_run is True


def test_debug_flag() -> None:
    args = parse_args(["https://sentry.io/issues/12345", "--debug"])
    assert args.debug is True


def test_all_flags() -> None:
    args = parse_args(["https://sentry.io/issues/12345", "--dry-run", "--debug"])
    assert args.dry_run is True
    assert args.debug is True


def test_missing_url_exits() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_repo_flag() -> None:
    args = parse_args(["https://sentry.io/issues/12345", "--repo", "https://github.com/org/repo"])
    assert args.urls == (
        URLInput(url="https://sentry.io/issues/12345", repo="https://github.com/org/repo"),
    )


def test_repo_flag_default_none() -> None:
    args = parse_args(["https://sentry.io/issues/12345"])
    assert args.urls[0].repo is None


def test_returns_cli_args() -> None:
    args = parse_args(["https://sentry.io/issues/1"])
    assert isinstance(args, CLIArgs)


def test_model_flag() -> None:
    args = parse_args(["https://sentry.io/issues/12345", "--model", "opus"])
    assert args.model == "opus"


def test_model_flag_default_none() -> None:
    args = parse_args(["https://sentry.io/issues/12345"])
    assert args.model is None


def test_invalid_model_exits() -> None:
    with pytest.raises(SystemExit):
        parse_args(["https://sentry.io/issues/12345", "--model", "gpt-4"])


def test_config_subcommand() -> None:
    args = parse_args(["config"])
    assert args.command == "config"


def test_config_subcommand_no_urls() -> None:
    args = parse_args(["config"])
    assert args.urls == ()


# -- Multi-URL parsing --


def test_multiple_urls_same_repo() -> None:
    args = parse_args(["url1", "url2", "url3", "--repo", "org/api"])
    assert args.urls == (
        URLInput(url="url1", repo="org/api"),
        URLInput(url="url2", repo="org/api"),
        URLInput(url="url3", repo="org/api"),
    )


def test_multiple_urls_different_repos() -> None:
    args = parse_args(["url1", "url2", "--repo", "org/api", "url3", "--repo", "org/billing"])
    assert args.urls == (
        URLInput(url="url1", repo="org/api"),
        URLInput(url="url2", repo="org/api"),
        URLInput(url="url3", repo="org/billing"),
    )


def test_partial_repo_binding() -> None:
    """URLs after the last --repo get repo=None."""
    args = parse_args(["url1", "--repo", "org/api", "url2", "url3"])
    assert args.urls == (
        URLInput(url="url1", repo="org/api"),
        URLInput(url="url2", repo=None),
        URLInput(url="url3", repo=None),
    )


def test_single_url_with_repo() -> None:
    args = parse_args(["url1", "--repo", "org/api"])
    assert args.urls == (URLInput(url="url1", repo="org/api"),)


def test_repo_without_preceding_urls_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--repo", "org/api", "url1"])


def test_repo_missing_value_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["url1", "--repo"])


def test_related_repo_flag() -> None:
    args = parse_args(
        ["url1", "--repo", "org/api", "--related-repo", "https://github.com/org/shared-lib"]
    )
    assert args.urls == (
        URLInput(
            url="url1",
            repo="org/api",
            related_repos=("https://github.com/org/shared-lib",),
        ),
    )


def test_related_repo_flag_repeatable() -> None:
    args = parse_args(
        [
            "url1",
            "--repo",
            "org/api",
            "--related-repo",
            "https://github.com/org/shared-lib",
            "--related-repo",
            "https://github.com/org/other-service",
        ]
    )
    assert args.urls[0].related_repos == (
        "https://github.com/org/shared-lib",
        "https://github.com/org/other-service",
    )


def test_related_repo_applies_to_all_preceding_urls() -> None:
    args = parse_args(["url1", "url2", "--related-repo", "https://github.com/org/shared-lib"])
    assert args.urls == (
        URLInput(url="url1", related_repos=("https://github.com/org/shared-lib",)),
        URLInput(url="url2", related_repos=("https://github.com/org/shared-lib",)),
    )


def test_related_repo_without_preceding_urls_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--related-repo", "org/api", "url1"])


def test_related_repo_missing_value_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["url1", "--related-repo"])


def test_model_missing_value_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["url1", "--model"])


def test_unknown_flag_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["url1", "--verbose"])


def test_help_flag_exits() -> None:
    with pytest.raises(SystemExit) as exc_info:
        parse_args(["--help"])
    assert exc_info.value.code == 0


def test_multi_url_with_flags() -> None:
    args = parse_args(["url1", "url2", "--repo", "org/api", "--dry-run", "--debug"])
    assert args.urls == (
        URLInput(url="url1", repo="org/api"),
        URLInput(url="url2", repo="org/api"),
    )
    assert args.dry_run is True
    assert args.debug is True


def test_urls_tuple_is_frozen() -> None:
    args = parse_args(["url1"])
    assert isinstance(args.urls, tuple)


# -- Explicit command --


def test_explicit_sentry_command() -> None:
    args = parse_args(["sentry", "https://sentry.io/issues/12345"])
    assert args.command == "run"
    assert args.adaptor_command == "sentry"
    assert args.urls == (URLInput(url="https://sentry.io/issues/12345"),)


def test_explicit_command_with_repo() -> None:
    args = parse_args(["sentry", "url1", "--repo", "org/api"])
    assert args.adaptor_command == "sentry"
    assert args.urls == (URLInput(url="url1", repo="org/api"),)


def test_explicit_command_without_urls_errors() -> None:
    with pytest.raises(SystemExit):
        parse_args(["sentry"])


def test_workflow_only_command_is_recognized() -> None:
    """ "echo" has no adaptor (src/adaptors/*/adaptor.py) behind it — only a
    ``command:echo`` workflow trigger (src/workflows/_smoke/echo.py) — so it
    must still be recognized as a command rather than parsed as a bare URL.
    """
    args = parse_args(["echo", "qa smoke 123"])
    assert args.command == "run"
    assert args.adaptor_command == "echo"
    assert args.urls == (URLInput(url="qa smoke 123"),)


# -- Backward compat --


def test_issues_alias() -> None:
    """args.issues is an alias for args.urls."""
    args = parse_args(["url1"])
    assert args.issues == args.urls
