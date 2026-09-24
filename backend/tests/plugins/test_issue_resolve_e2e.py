"""issue_resolve end to end with a mocked CLI: dispatch → container logs → status consumer → stats.

The CLI is replaced by the stdout it prints (``[JEANCLODE:RESULT]`` lines,
shaped like ``cli/src/runner/run.py`` emits them); everything from log
parsing onward is the real backend path.
"""

import importlib
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from api.database import db_create_workspace, db_create_workspace_membership
from api.database.pull_request import db_git_org_has_open_fix_pr
from api.models.issues import Issue
from api.models.organizations import Organization
from api.models.pull_requests import PullRequest
from api.models.repositories import Repository
from api.plugins.container.utils import determine_execution_status, parse_structured_logs

HOSTS = {"github": "https://github.com", "gitlab": "https://gitlab.example.com"}
PR_PATHS = {"github": "pull", "gitlab": "-/merge_requests"}


def _cli_stdout(data: dict, *, status: str = "success") -> str:
    """What the issue_resolve CLI prints, down to the tagged-line format."""
    result = {
        "status": status,
        "workflow": "issue_resolve",
        "summary": "mocked",
        "data": data,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    return "\n".join(
        [
            "[2026-09-24 10:00:00] [JEANCLODE:STEP] "
            + json.dumps({"step": "workflow", "status": "started"}),
            "some agent chatter",
            "[2026-09-24 10:05:00] [JEANCLODE:RESULT] " + json.dumps(result),
        ]
    )


@pytest.fixture
def no_side_effects():
    broker = AsyncMock()
    with (
        patch("api.routers.issues.route.get_faststream_broker", return_value=broker),
        patch("api.routers.issues.route.publish_execution_event", new=AsyncMock()),
        patch("api.plugins.github.consumer._sync_status_comments_for_execution", new=AsyncMock()),
        patch("api.plugins.gitlab.consumer._sync_status_comments_for_execution", new=AsyncMock()),
        patch("api.sse.stats_cache._redis", side_effect=RuntimeError("no cache in this test")),
    ):
        yield broker


def _seed(db, user_id, provider):
    name = f"e2e-{uuid.uuid4().hex[:6]}"
    ws = db_create_workspace(db=db, name=name, slug=name)
    db_create_workspace_membership(db=db, workspace_id=ws.id, user_id=user_id)
    org = Organization(workspace_id=ws.id, name="acme", external_org_id=name, provider=provider)
    db.add(org)
    db.flush()
    repo = Repository(
        org_id=org.id,
        name="acme/app",
        external_id=name,
        provider=provider,
        web_url=f"{HOSTS[provider]}/acme/app",
    )
    db.add(repo)
    db.flush()
    issues = []
    for number in (12, 13):
        issue = Issue(
            repository_id=repo.id,
            external_id=str(number),
            title=f"Issue {number}",
            level="info",
            status="open",
        )
        db.add(issue)
        issues.append(issue)
    db.commit()
    return str(ws.id), org.id, [str(i.id) for i in issues]


async def _run_mocked_cli(provider: str, execution_id: str, stdout: str, exit_code: int):
    consumer = importlib.import_module(f"api.plugins.{provider}.consumer")
    result, error_info, _ = parse_structured_logs(stdout)
    status, error_info = determine_execution_status(exit_code, error_info)
    await consumer._handle_execution_status(
        consumer.ExecutionStatusMessage(
            execution_id=execution_id,
            status=status,
            exit_code=exit_code,
            error_type=error_info[0] if error_info else None,
            error_message=error_info[1] if error_info else None,
            result=result,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_issue_resolve_run_lands_in_issues_and_stats(
    auth_client, app, mock_auth, no_side_effects, provider
):
    with app.database.session() as db:
        ws_id, org_id, (fixed_id, dismissed_id) = _seed(db, mock_auth.id, provider)

    fixed_exec = auth_client.post(f"/issues/{fixed_id}/resolve").json()["execution_id"]
    dismissed_exec = auth_client.post(f"/issues/{dismissed_id}/resolve").json()["execution_id"]
    assert no_side_effects.publish.await_count == 2

    pr_url = f"{HOSTS[provider]}/acme/app/{PR_PATHS[provider]}/41"
    await _run_mocked_cli(
        provider,
        fixed_exec,
        _cli_stdout(
            {
                "kind": "proceed",
                "triage_result": "actionable",
                "pr_urls": [pr_url],
                "repos": {"acme/app": {"pr_url": pr_url, "ci": "passed"}},
            }
        ),
        exit_code=0,
    )
    await _run_mocked_cli(
        provider,
        dismissed_exec,
        _cli_stdout({"kind": "not_actionable", "triage_result": "not_actionable"}),
        exit_code=0,
    )

    issues = auth_client.get("/issues", params={"workspace_id": ws_id}).json()["objects"]
    by_id = {i["id"]: i for i in issues}
    assert by_id[fixed_id]["result"] == "pr_open"
    assert by_id[fixed_id]["triage_result"] == "actionable"
    assert by_id[dismissed_id]["result"] == "not_actionable"

    stats = auth_client.get(f"/workspaces/{ws_id}/stats").json()
    assert stats["issues"] == {"handled": 2, "prs_created": 1, "prs_merged": 0}
    assert stats["dashboard"]["successful_runs"] == 2
    assert stats["pull_requests"]["pending_review"] == 1

    with app.database.session() as db:
        pr = db.query(PullRequest).one()
        assert (pr.pr_number, pr.pr_url) == (41, pr_url)
        # An issue_resolve PR is not a Sentry fix PR: the merge gate stays open.
        assert db_git_org_has_open_fix_pr(db, org_id) is False
        pr.state = "merged"
        db.commit()

    stats = auth_client.get(f"/workspaces/{ws_id}/stats").json()
    assert stats["issues"]["prs_merged"] == 1
    assert stats["pull_requests"]["pending_review"] == 0


@pytest.mark.asyncio
async def test_failed_fix_still_records_the_pr_it_opened(
    auth_client, app, mock_auth, no_side_effects
):
    with app.database.session() as db:
        ws_id, _org_id, (issue_id, _) = _seed(db, mock_auth.id, "github")

    execution_id = auth_client.post(f"/issues/{issue_id}/resolve").json()["execution_id"]
    pr_url = "https://github.com/acme/app/pull/9"
    await _run_mocked_cli(
        "github",
        execution_id,
        _cli_stdout(
            {"kind": "proceed", "triage_result": "actionable", "pr_urls": [pr_url], "error": "x"},
            status="error",
        ),
        exit_code=1,
    )

    issue = next(
        i
        for i in auth_client.get("/issues", params={"workspace_id": ws_id}).json()["objects"]
        if i["id"] == issue_id
    )
    assert issue["execution_status"] == "failed"
    stats = auth_client.get(f"/workspaces/{ws_id}/stats").json()
    assert stats["issues"]["prs_created"] == 1
    assert stats["dashboard"]["successful_runs"] == 0
