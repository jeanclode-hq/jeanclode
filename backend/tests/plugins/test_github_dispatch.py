"""Tests for the github webhook trigger evaluator.

Review/summary auto-dispatch only ever fires on its own
``jeanclode:<workflow>`` label being added — never on PR creation or a
new commit, and there is no per-org setting to change that.
"""

from __future__ import annotations

import pytest

from api.models.executions import ExecutionWorkflow
from api.plugins.github.dispatch import (
    LABEL_RESOLVE,
    LABEL_REVIEW,
    LABEL_SUMMARY,
    evaluate_trigger,
)
from api.plugins.gitlab import dispatch as gitlab_dispatch

# ── REVIEW workflow ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "action,label_added,expected",
    [
        ("opened", None, False),
        ("synchronize", None, False),
        ("labeled", LABEL_REVIEW, True),
        ("labeled", "something-else", False),
        ("labeled", LABEL_SUMMARY, False),
    ],
)
def test_review_trigger_matrix(action, label_added, expected):
    assert (
        evaluate_trigger(ExecutionWorkflow.REVIEW, action=action, label_added=label_added)
        is expected
    )


# ── SUMMARY workflow ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "action,label_added,expected",
    [
        ("opened", None, False),
        ("synchronize", None, False),
        ("labeled", LABEL_SUMMARY, True),
        # Summary trigger ignores the review label even when added
        ("labeled", LABEL_REVIEW, False),
    ],
)
def test_summary_trigger_matrix(action, label_added, expected):
    assert (
        evaluate_trigger(ExecutionWorkflow.SUMMARY, action=action, label_added=label_added)
        is expected
    )


def test_resolve_label_follows_naming_convention():
    """jeanclode:<workflow> convention, re-exported by the gitlab evaluator."""
    assert LABEL_RESOLVE == "jeanclode:resolve"
    assert gitlab_dispatch.LABEL_RESOLVE is LABEL_RESOLVE


def test_fix_workflow_never_dispatched_via_pr_evaluator():
    """FIX is issue-driven; the PR evaluator must always return False for it."""
    assert (
        evaluate_trigger(ExecutionWorkflow.FIX, action="labeled", label_added=LABEL_REVIEW) is False
    )
