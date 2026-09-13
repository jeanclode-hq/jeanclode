"""Add ``provider`` column to executions.

Scopes the reconciler's stale-cleanup query to a single container plugin
(github / gitlab / sentry). Without it, a github reconciler tick can scan
all queued/running executions and false-fail sentry-owned ones, and vice
versa (#104).

Revision ID: 8e1f2a3b4c5d
Revises: 7d0e1f2a3b4c
Create Date: 2026-05-05 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "8e1f2a3b4c5d"
down_revision: Union[str, None] = "7d0e1f2a3b4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("provider", sa.String(length=50), nullable=False, server_default=""),
    )
    op.create_index("ix_executions_provider_status", "executions", ["provider", "status"])


def downgrade() -> None:
    op.drop_index("ix_executions_provider_status", table_name="executions")
    op.drop_column("executions", "provider")
