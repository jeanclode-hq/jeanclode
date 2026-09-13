"""Utility functions for pull requests endpoints."""

from api.models.settings import RepoSettings

from .schemas import PullRequestResponse


def row_to_pr_response(row) -> PullRequestResponse:
    """Convert a query result row to PullRequestResponse."""
    pr = row[0]
    exec_id = row.exec_id if hasattr(row, "exec_id") else None
    exec_status = row.exec_status if hasattr(row, "exec_status") else None
    exec_workflow = row.exec_workflow if hasattr(row, "exec_workflow") else None
    exec_error_type = row.exec_error_type if hasattr(row, "exec_error_type") else None
    exec_error_detail = row.exec_error_detail if hasattr(row, "exec_error_detail") else None
    exec_trigger = row.exec_trigger if hasattr(row, "exec_trigger") else None
    exec_created_at = row.exec_created_at if hasattr(row, "exec_created_at") else None
    repo_provider = row.repo_provider if hasattr(row, "repo_provider") else None

    repo = pr.repository
    repo_settings = RepoSettings.model_validate(repo.settings if repo else {})

    return PullRequestResponse(
        id=pr.id,
        title=pr.title,
        author=pr.author,
        pr_url=pr.pr_url,
        pr_number=pr.pr_number,
        branch_name=pr.head_branch,
        repo_name=repo.name if repo else "",
        provider=repo_provider or (repo.provider if repo else ""),
        status=pr.state,
        execution_status=exec_status or "none",
        execution_id=exec_id,
        workflow=exec_workflow,
        error_type=exec_error_type,
        error_detail=exec_error_detail,
        execution_trigger=exec_trigger,
        execution_started_at=exec_created_at,
        repo_enabled=repo_settings.enabled,
        created_at=pr.created_at,
        merged_at=pr.updated_at if pr.state == "merged" else None,
    )
