"""Refactor Execution-target relationships to many-to-many.

Replaces the single FK columns ``executions.issue_id`` and
``executions.pull_request_id`` (with the ``ck_execution_has_target`` check
constraint) with two association tables:

- ``execution_issues (execution_id, issue_id)`` — for FIX-workflow batches
- ``execution_pull_requests (execution_id, pull_request_id)`` — for REVIEW/SUMMARY

Revision ID: 7d0e1f2a3b4c
Revises: 6c9d0e1f2a3b
Create Date: 2026-04-29 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "7d0e1f2a3b4c"
down_revision: Union[str, None] = "6c9d0e1f2a3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "execution_issues",
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("issue_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("execution_id", "issue_id", name="pk_execution_issues"),
    )
    op.create_index("ix_execution_issues_issue_id", "execution_issues", ["issue_id"])

    op.create_table(
        "execution_pull_requests",
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("pull_request_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["execution_id"], ["executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "execution_id", "pull_request_id", name="pk_execution_pull_requests"
        ),
    )
    op.create_index(
        "ix_execution_pull_requests_pull_request_id",
        "execution_pull_requests",
        ["pull_request_id"],
    )

    op.execute(
        """
        INSERT INTO execution_issues (execution_id, issue_id, created_at)
        SELECT id, issue_id, created_at FROM executions WHERE issue_id IS NOT NULL
        """
    )
    op.execute(
        """
        INSERT INTO execution_pull_requests (execution_id, pull_request_id, created_at)
        SELECT id, pull_request_id, created_at
        FROM executions WHERE pull_request_id IS NOT NULL
        """
    )

    op.drop_constraint("ck_execution_has_target", "executions", type_="check")
    op.drop_index("ix_executions_issue_id", table_name="executions")
    op.drop_index("ix_executions_pull_request_id", table_name="executions")
    op.drop_column("executions", "issue_id")
    op.drop_column("executions", "pull_request_id")


def downgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("issue_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("pull_request_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "executions_issue_id_fkey",
        "executions",
        "issues",
        ["issue_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "executions_pull_request_id_fkey",
        "executions",
        "pull_requests",
        ["pull_request_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Backfill from join tables. Lossy if any execution links >1 target —
    # the M2M model allows this but the scalar columns can hold only one.
    op.execute(
        """
        UPDATE executions e SET issue_id = ei.issue_id
        FROM (
            SELECT execution_id, MIN(issue_id) AS issue_id
            FROM execution_issues GROUP BY execution_id
        ) ei
        WHERE e.id = ei.execution_id
        """
    )
    op.execute(
        """
        UPDATE executions e SET pull_request_id = epr.pull_request_id
        FROM (
            SELECT execution_id, MIN(pull_request_id) AS pull_request_id
            FROM execution_pull_requests GROUP BY execution_id
        ) epr
        WHERE e.id = epr.execution_id
        """
    )

    op.create_index("ix_executions_issue_id", "executions", ["issue_id"])
    op.create_index("ix_executions_pull_request_id", "executions", ["pull_request_id"])
    op.create_check_constraint(
        "ck_execution_has_target",
        "executions",
        "issue_id IS NOT NULL OR pull_request_id IS NOT NULL",
    )

    op.drop_index(
        "ix_execution_pull_requests_pull_request_id", table_name="execution_pull_requests"
    )
    op.drop_table("execution_pull_requests")
    op.drop_index("ix_execution_issues_issue_id", table_name="execution_issues")
    op.drop_table("execution_issues")
