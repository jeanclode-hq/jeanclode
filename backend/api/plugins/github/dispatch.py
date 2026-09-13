"""GitHub webhook → workflow dispatch evaluator.

GitHub doesn't need a polling dispatcher (unlike Sentry) — every relevant
state change ships as a webhook. This module owns the rule that decides
whether a given pull_request webhook event should fire a workflow
(REVIEW / SUMMARY): only the ``jeanclode:<workflow>`` label, added to the
PR, ever triggers one — never PR creation or a new commit on its own.

Convention for label-based triggers: the label name must be
``jeanclode:<workflow>`` (e.g. ``jeanclode:review``). Hardcoded; there is
no per-org trigger-mode setting to make it configurable.
"""

from __future__ import annotations

import logging

from api.models.executions import ExecutionWorkflow

logger = logging.getLogger(__name__)


# Hardcoded label names that gate dispatch. Adding configurability here
# means a settings schema change — keep in sync with the gitlab evaluator
# if/when that happens.
LABEL_REVIEW = "jeanclode:review"
LABEL_SUMMARY = "jeanclode:summary"
LABEL_RESOLVE = "jeanclode:resolve"


def _label_for(workflow: ExecutionWorkflow) -> str:
    if workflow == ExecutionWorkflow.SUMMARY:
        return LABEL_SUMMARY
    return LABEL_REVIEW


def evaluate_trigger(
    workflow: ExecutionWorkflow,
    *,
    action: str,
    label_added: str | None = None,
) -> bool:
    """Decide whether a pull_request webhook event should dispatch ``workflow``.

    Args:
        workflow: REVIEW or SUMMARY (FIX is not webhook-driven on PRs).
        action: GitHub action string (``opened``, ``synchronize``,
            ``labeled``, …) or the GitLab equivalent normalized to the
            same vocabulary by the caller.
        label_added: When ``action == "labeled"``, the specific label
            that was just added — distinguishes it from any other label.

    Returns:
        True iff ``action`` is ``labeled`` and the label added is the one
        that gates ``workflow``.
    """
    if workflow == ExecutionWorkflow.FIX:
        return False
    return action == "labeled" and label_added == _label_for(workflow)
