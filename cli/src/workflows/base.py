"""Workflow protocol and the global registry.

The data model (`WorkflowResult`) lives in `schemas.py`; small string
helpers live in `utils.py`. This module owns only the protocol +
registry surface.
"""

import fnmatch
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

from src.workflows.schemas import WorkflowResult
from src.workflows.utils import strip_url_scheme

if TYPE_CHECKING:
    from src.runtime.context import RunContext


@runtime_checkable
class Workflow(Protocol):
    """A Python-driven workflow.

    Implementations declare static metadata as ClassVars and an async
    `run(ctx)` method. The orchestrator instantiates the class with no
    arguments and awaits `.run(ctx)`.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    triggers: ClassVar[list[str]]

    async def run(self, ctx: RunContext) -> WorkflowResult: ...


WORKFLOWS: dict[str, type[Workflow]] = {}


def register[W: Workflow](cls: type[W]) -> type[W]:
    """Register a Workflow class in the global registry.

    Validates the contract by attribute access — raising `TypeError` early
    is friendlier than a confusing `AttributeError` at dispatch time.
    """
    for attr in ("name", "description", "triggers", "run"):
        if not hasattr(cls, attr):
            msg = f"{cls.__name__} is not a valid Workflow: missing '{attr}'"
            raise TypeError(msg)

    WORKFLOWS[cls.name] = cls
    return cls


def command_names() -> set[str]:
    """Workflow-only command triggers (e.g. ``echo``) with no adaptor.

    Adaptor skills (``src/adaptors/*/adaptor.py``) cover URL-driven
    commands like ``review``/``summary``/``respond``. A workflow that
    triggers purely on ``command:<name>`` with no adaptor behind it (the
    ``echo`` smoke workflow) would otherwise never be recognized as a
    valid CLI subcommand by ``cli.py``'s ``known_commands`` check.
    """
    return {
        trigger.removeprefix("command:")
        for cls in WORKFLOWS.values()
        for trigger in cls.triggers
        if trigger.startswith("command:")
    }


def find_workflow(
    *,
    url: str | None = None,
    command: str | None = None,
) -> type[Workflow] | None:
    """Look up a workflow by URL match or command match.

    Triggers are either:
      - URL globs matched with fnmatch (e.g. ``sentry.io/issues/*``)
      - ``command:<name>`` literals matched against ``command``

    Command matches take precedence over URL matches when both are given.
    """
    if command is not None:
        wanted = f"command:{command}"
        for cls in WORKFLOWS.values():
            if wanted in cls.triggers:
                return cls

    if url is not None:
        normalized = strip_url_scheme(url)
        for cls in WORKFLOWS.values():
            for trigger in cls.triggers:
                if trigger.startswith("command:"):
                    continue
                if fnmatch.fnmatch(normalized, trigger):
                    return cls

    return None
