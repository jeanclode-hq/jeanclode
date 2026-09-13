"""Workflow registry behaviour."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

import pytest

from src.runtime.context import RunContext
from src.workflows.base import WORKFLOWS, find_workflow, register
from src.workflows.schemas import WorkflowResult


@pytest.fixture
def isolated_registry() -> Iterator[None]:
    snapshot = dict(WORKFLOWS)
    WORKFLOWS.clear()
    try:
        yield
    finally:
        WORKFLOWS.clear()
        WORKFLOWS.update(snapshot)


def test_register_populates_registry(isolated_registry: None) -> None:
    @register
    class _Foo:
        name: ClassVar[str] = "foo"
        description: ClassVar[str] = "a foo"
        triggers: ClassVar[list[str]] = ["command:foo"]

        async def run(self, ctx: RunContext) -> WorkflowResult:
            return WorkflowResult()

    assert WORKFLOWS["foo"] is _Foo


def test_register_rejects_non_workflow(isolated_registry: None) -> None:
    class _Broken:
        pass

    with pytest.raises(TypeError, match="not a valid Workflow"):
        register(_Broken)


def test_find_workflow_by_command(isolated_registry: None) -> None:
    @register
    class _Foo:
        name: ClassVar[str] = "foo"
        description: ClassVar[str] = ""
        triggers: ClassVar[list[str]] = ["command:foo"]

        async def run(self, ctx: RunContext) -> WorkflowResult:
            return WorkflowResult()

    assert find_workflow(command="foo") is _Foo
    assert find_workflow(command="missing") is None


def test_find_workflow_by_url_glob(isolated_registry: None) -> None:
    @register
    class _Sentry:
        name: ClassVar[str] = "sentry"
        description: ClassVar[str] = ""
        triggers: ClassVar[list[str]] = ["sentry.io/issues/*"]

        async def run(self, ctx: RunContext) -> WorkflowResult:
            return WorkflowResult()

    assert find_workflow(url="https://sentry.io/issues/12345") is _Sentry
    assert find_workflow(url="https://github.com/org/repo") is None


def test_find_workflow_command_takes_precedence(isolated_registry: None) -> None:
    @register
    class _A:
        name: ClassVar[str] = "a"
        description: ClassVar[str] = ""
        triggers: ClassVar[list[str]] = ["sentry.io/*"]

        async def run(self, ctx: RunContext) -> WorkflowResult:
            return WorkflowResult()

    @register
    class _B:
        name: ClassVar[str] = "b"
        description: ClassVar[str] = ""
        triggers: ClassVar[list[str]] = ["command:b"]

        async def run(self, ctx: RunContext) -> WorkflowResult:
            return WorkflowResult()

    assert find_workflow(url="https://sentry.io/issues/1", command="b") is _B
