"""Python-driven workflows.

Workflows replace the model-orchestrated `SKILL.md` pattern: control flow
lives in Python (Workflow.run), state lives in a Pydantic RunContext, and
LLM calls are encapsulated in BaseAgent activities.

First-party workflows are imported here so the `@register` decorator
fires on package import — `find_workflow(...)` then sees them without
any further setup. Adding a new workflow is one new import line.
"""

# Side-effect imports — each module's @register populates WORKFLOWS.
from src.workflows import jeanclode_respond as _jeanclode_respond
from src.workflows._smoke import echo as _echo
from src.workflows.base import WORKFLOWS, Workflow, command_names, find_workflow, register
from src.workflows.code_review import runner as _code_review
from src.workflows.issue_resolve import runner as _issue_resolve
from src.workflows.pr_summary import runner as _pr_summary
from src.workflows.schemas import WorkflowResult
from src.workflows.sentry_fix import runner as _sentry_fix

__all__ = [
    "WORKFLOWS",
    "Workflow",
    "WorkflowResult",
    "command_names",
    "find_workflow",
    "register",
]
