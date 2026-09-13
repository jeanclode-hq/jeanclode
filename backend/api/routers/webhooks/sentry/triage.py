"""Issue processing triage logic.

Pure function that determines whether a Sentry issue should be processed,
skipped, or retried based on its current state and incoming event data.

Ref: ADR-001 — Issue Processing Decision Logic
"""

from api.models.issues import TriageResult
from api.routers.webhooks.sentry.schemas import Decision, IssueRecord, SentryEvent

# Maximum number of retry attempts for failed processing
MAX_RETRIES = 3


# TODO: Regression detection — needs its own Decision variant (e.g. RE_TRIAGE)
#  so the consumer can distinguish "brand new issue" from "fix didn't work".
#  Requires release tag comparison via _is_fix_deployed().
#  See ADR-001 scenario 7.
#
# def _is_fix_deployed(fix_release: str, event_release: str) -> bool:
#     return event_release >= fix_release
#
# def _decide_completed(record: IssueRecord, event: SentryEvent) -> Decision:
#     if event.release is None:
#         return Decision.SKIP
#     if record.fix_release is None:
#         return Decision.SKIP
#     if _is_fix_deployed(record.fix_release, event.release):
#         return Decision.RE_TRIAGE
#     return Decision.SKIP


def decide(issue_record: IssueRecord | None, sentry_event: SentryEvent) -> Decision:
    """Determine how to handle an incoming Sentry issue event.

    Args:
        issue_record: Existing issue state, or ``None`` for a brand-new issue.
        sentry_event: Data from the incoming Sentry webhook.

    Returns:
        A :class:`Decision` indicating the action to take.

    Decision tree (see ADR-001):
        - Never seen before                         → ACCEPT
        - Not actionable                            → SKIP
        - Has active execution (queued/running)      → SKIP
        - Has completed execution (PR created/merged) → SKIP (TODO: regression)
        - All executions failed, under retry cap     → RETRY
        - All executions failed, at retry cap        → SKIP
        - Actionable, no executions yet              → SKIP (already queued for dispatch)
    """
    if issue_record is None:
        return Decision.ACCEPT

    # Not actionable — triage said skip
    if issue_record.triage_result == TriageResult.NOT_ACTIONABLE:
        return Decision.SKIP

    # Has an active execution running
    if issue_record.has_active_execution:
        return Decision.SKIP

    # Has a completed execution (PR was created)
    if issue_record.has_completed_execution:
        return Decision.SKIP  # TODO: regression detection

    # All executions failed — retry with cap
    if issue_record.failed_execution_count > 0:
        if issue_record.failed_execution_count >= MAX_RETRIES:
            return Decision.SKIP
        return Decision.RETRY

    # Actionable with no executions — already queued for dispatch
    if issue_record.triage_result == TriageResult.ACTIONABLE:
        return Decision.SKIP

    # Brand new — should have been caught by None check, but handle gracefully
    return Decision.ACCEPT
