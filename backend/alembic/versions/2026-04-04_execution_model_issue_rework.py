"""execution model and issue triage rework

Revision ID: 6ac64bc275b5
Revises: 74f8eac09344
Create Date: 2026-04-04 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6ac64bc275b5"
down_revision: Union[str, Sequence[str], None] = "74f8eac09344"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create executions table and rework issues to use triage_result."""
    # 1. Create executions table
    op.create_table(
        "executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "issue_id",
            sa.Uuid(),
            sa.ForeignKey("issues.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "pull_request_id",
            sa.Uuid(),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("workflow", sa.String(50), nullable=False, server_default="fix"),
        sa.Column("status", sa.String(50), nullable=False, server_default="queued"),
        sa.Column("container_id", sa.String(255), nullable=True),
        sa.Column("error_type", sa.String(255), nullable=True),
        sa.Column("error_detail", sa.String(2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "issue_id IS NOT NULL OR pull_request_id IS NOT NULL",
            name="ck_execution_has_target",
        ),
    )

    op.create_index("ix_executions_issue_id", "executions", ["issue_id"])
    op.create_index("ix_executions_pull_request_id", "executions", ["pull_request_id"])
    op.create_index("ix_executions_status", "executions", ["status"])
    op.create_index("ix_executions_created_at", "executions", ["created_at"])

    # 2. Rename issue columns to be source-agnostic
    op.alter_column("issues", "triage_result", new_column_name="triage_metadata")
    op.alter_column("issues", "sentry_project_id", new_column_name="source_project_id")
    op.alter_column("issues", "sentry_issue_id", new_column_name="external_id")

    # Rename indexes and constraints
    op.drop_constraint("uq_sentry_project_issue", "issues", type_="unique")
    op.create_unique_constraint(
        "uq_issue_source_project_external", "issues", ["source_project_id", "external_id"]
    )
    op.drop_index("ix_issues_sentry_project_id", table_name="issues")
    op.create_index("ix_issues_source_project_id", "issues", ["source_project_id"])

    # 3. Add issues.triage_result (String) for the enum value
    op.add_column(
        "issues",
        sa.Column("triage_result", sa.String(50), nullable=True),
    )
    op.add_column(
        "issues",
        sa.Column("issue_url", sa.String(500), nullable=True),
    )
    op.add_column(
        "issues",
        sa.Column("author", sa.String(255), nullable=True),
    )

    # 4. Data migration: convert existing status values to triage_result + execution rows
    #
    # Map issue status → triage_result:
    #   pending       → actionable
    #   running       → actionable  (+ create running execution)
    #   pr_open       → actionable  (+ create completed execution)
    #   pr_merged     → actionable  (+ create completed execution)
    #   not_actionable→ not_actionable
    #   rejected      → actionable  (+ create completed execution)
    #   failed        → actionable  (+ create failed execution per retry_count)

    # Set triage_result for all issues
    op.execute("""
        UPDATE issues SET triage_result = 'actionable'
        WHERE status IN ('pending', 'running', 'pr_open', 'pr_merged', 'rejected', 'failed')
    """)
    op.execute("""
        UPDATE issues SET triage_result = 'not_actionable'
        WHERE status = 'not_actionable'
    """)

    # Create execution records for running issues
    op.execute("""
        INSERT INTO executions (id, issue_id, workflow, status, created_at, updated_at)
        SELECT gen_random_uuid(), id, 'fix', 'running', NOW(), NOW()
        FROM issues WHERE status = 'running'
    """)

    # Create execution records for completed issues (pr_open, pr_merged, rejected)
    op.execute("""
        INSERT INTO executions (id, issue_id, workflow, status, created_at, updated_at)
        SELECT gen_random_uuid(), id, 'fix', 'completed', updated_at, updated_at
        FROM issues WHERE status IN ('pr_open', 'pr_merged', 'rejected')
    """)

    # Create execution records for failed issues (one per retry_count)
    # For simplicity, create one failed execution with the count info
    op.execute("""
        INSERT INTO executions (id, issue_id, workflow, status, error_type, created_at, updated_at)
        SELECT gen_random_uuid(), id, 'fix', 'failed', 'legacy_migration', updated_at, updated_at
        FROM issues WHERE status = 'failed'
    """)

    # 5. Drop old columns from issues
    op.drop_column("issues", "retry_count")
    op.drop_column("issues", "pr_url")
    op.drop_column("issues", "pr_number")
    op.drop_column("issues", "branch_name")

    # 6. Convert old status to new external status + triage_result
    # Old: pending/running/pr_open/pr_merged/not_actionable/rejected/failed
    # New status: unresolved/resolved (external source status)
    # New triage_result: pending/actionable/not_actionable
    op.execute("""
        UPDATE issues SET triage_result = CASE
            WHEN status = 'not_actionable' THEN 'not_actionable'
            WHEN status IN ('pending', 'running', 'pr_open', 'pr_merged', 'rejected', 'failed') THEN 'actionable'
            ELSE 'pending'
        END
    """)
    op.execute("UPDATE issues SET status = 'unresolved'")

    # 7. Add new indexes
    op.create_index("ix_issues_triage_result", "issues", ["triage_result"])


def downgrade() -> None:
    """Reverse: restore issues.status and drop executions table."""
    # Re-add dropped columns (status stays but gets old values)
    op.add_column("issues", sa.Column("retry_count", sa.Integer(), server_default="0"))
    op.add_column("issues", sa.Column("pr_url", sa.String(500), nullable=True))
    op.add_column("issues", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("issues", sa.Column("branch_name", sa.String(255), nullable=True))

    # Reverse triage_result → status mapping
    op.execute("""
        UPDATE issues SET status = CASE
            WHEN triage_result = 'not_actionable' THEN 'not_actionable'
            ELSE 'pending'
        END
    """)

    op.drop_index("ix_issues_triage_result", table_name="issues")
    op.create_index("ix_issues_status", "issues", ["status"])

    # Reverse column renames
    op.drop_column("issues", "author")
    op.drop_column("issues", "issue_url")
    op.drop_column("issues", "triage_result")
    op.alter_column("issues", "triage_metadata", new_column_name="triage_result")
    op.alter_column("issues", "source_project_id", new_column_name="sentry_project_id")
    op.alter_column("issues", "external_id", new_column_name="sentry_issue_id")

    # Restore original constraints/indexes
    op.drop_constraint("uq_issue_source_project_external", "issues", type_="unique")
    op.create_unique_constraint(
        "uq_sentry_project_issue", "issues", ["sentry_project_id", "sentry_issue_id"]
    )
    op.drop_index("ix_issues_source_project_id", table_name="issues")
    op.create_index("ix_issues_sentry_project_id", "issues", ["sentry_project_id"])

    # Drop executions table
    op.drop_index("ix_executions_created_at", table_name="executions")
    op.drop_index("ix_executions_status", table_name="executions")
    op.drop_index("ix_executions_pull_request_id", table_name="executions")
    op.drop_index("ix_executions_issue_id", table_name="executions")
    op.drop_table("executions")
