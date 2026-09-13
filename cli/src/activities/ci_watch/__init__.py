"""ci-watch — verify a pushed fix against the target repo's own CI.

Enforced via ``src.agents.hooks.require_ci_pass_hook`` — a PreToolUse hook on
the schema-bound fixer's ``StructuredOutput`` call, not an agent-callable
tool (a tool call is something a model can skip; this hook fires on every
attempt to finalize the turn and can't be opted out of). It gates
``StructuredOutput`` rather than ``Stop`` because a ``Stop`` block lands
after the structured output is already submitted and the CLI drops it. See
``docs/adr/008-ci-gated-fix-verification.md`` for the design rationale.
"""

from src.activities.ci_watch.poll import check_ci
from src.activities.ci_watch.schemas import CheckResult, CiWatchResult

__all__ = [
    "CheckResult",
    "CiWatchResult",
    "check_ci",
]
