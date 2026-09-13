"""Add issue_pull_requests link table.

A ``fix`` execution batches several Sentry issues, and synthesis can split
that batch into multiple PRs — one per root-cause group. ``execution_issues``
and ``execution_pull_requests`` only say "these issues and these PRs came out
of this execution," not which PR addresses which issue, so an issue's detail
view was showing every PR the whole batch opened, including ones addressing
unrelated issues in the same batch. This table records the precise mapping.

Revision ID: 972714edb971
Revises: a4d7e2c91b83
Create Date: 2026-09-11 14:05:20.763646

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "972714edb971"
down_revision: Union[str, None] = "a4d7e2c91b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "issue_pull_requests",
        sa.Column("issue_id", sa.UUID(), nullable=False),
        sa.Column("pull_request_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pull_request_id"], ["pull_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("issue_id", "pull_request_id", name="pk_issue_pull_requests"),
    )
    op.create_index(
        "ix_issue_pull_requests_pull_request_id", "issue_pull_requests", ["pull_request_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_issue_pull_requests_pull_request_id", table_name="issue_pull_requests")
    op.drop_table("issue_pull_requests")
