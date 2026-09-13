"""Tests for the run() dispatch gate — LOCAL_ALLOWED_WORKFLOWS whitelist."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.cli import CLIArgs, URLInput
from src.runner.run import run


@pytest.mark.parametrize(
    "url",
    ["https://sentry.io/issues/12345", "https://github.com/org/repo/issues/99"],
)
async def test_container_only_workflow_rejected_locally(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.delenv("JEANCLODE_CONTAINER_MODE", raising=False)
    args = CLIArgs(command="run", urls=(URLInput(url=url),))

    with patch("src.runner.run._run_workflow", new_callable=AsyncMock) as run_workflow:
        code = await run(args)

    assert code == 1
    run_workflow.assert_not_called()


@pytest.mark.parametrize(
    "url",
    ["https://sentry.io/issues/12345", "https://github.com/org/repo/issues/99"],
)
async def test_container_only_workflow_allowed_in_container_mode(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("JEANCLODE_CONTAINER_MODE", "1")
    args = CLIArgs(command="run", urls=(URLInput(url=url),))

    with patch(
        "src.runner.run._run_workflow", new_callable=AsyncMock, return_value=0
    ) as run_workflow:
        code = await run(args)

    assert code == 0
    run_workflow.assert_called_once()


async def test_respond_rejected_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_CONTAINER_MODE", raising=False)
    args = CLIArgs(
        command="run",
        adaptor_command="respond",
        urls=(URLInput(url="https://github.com/org/repo/pull/1#issuecomment-1"),),
    )

    with patch("src.runner.run._run_workflow", new_callable=AsyncMock) as run_workflow:
        code = await run(args)

    assert code == 1
    run_workflow.assert_not_called()


async def test_respond_allowed_in_container_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEANCLODE_CONTAINER_MODE", "1")
    args = CLIArgs(
        command="run",
        adaptor_command="respond",
        urls=(URLInput(url="https://github.com/org/repo/pull/1#issuecomment-1"),),
    )

    with patch(
        "src.runner.run._run_workflow", new_callable=AsyncMock, return_value=0
    ) as run_workflow:
        code = await run(args)

    assert code == 0
    run_workflow.assert_called_once()


async def test_review_workflow_still_allowed_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_CONTAINER_MODE", raising=False)
    args = CLIArgs(command="run", urls=(URLInput(url="https://github.com/org/repo/pull/1"),))

    with patch(
        "src.runner.run._run_workflow", new_callable=AsyncMock, return_value=0
    ) as run_workflow:
        code = await run(args)

    assert code == 0
    run_workflow.assert_called_once()


async def test_echo_workflow_still_allowed_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEANCLODE_CONTAINER_MODE", raising=False)
    args = CLIArgs(command="run", adaptor_command="echo", urls=(URLInput(url="qa smoke"),))

    with patch(
        "src.runner.run._run_workflow", new_callable=AsyncMock, return_value=0
    ) as run_workflow:
        code = await run(args)

    assert code == 0
    run_workflow.assert_called_once()
