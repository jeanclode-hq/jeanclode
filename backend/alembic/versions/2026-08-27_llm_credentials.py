"""Add llm_credentials table and executions.retry_at (ADR-010).

Replaces the single ``llm`` instance-settings row with a priority-ordered
pool of credentials. Schema only — no data backfill: existing instances
re-enter their LLM credentials through the admin UI after upgrade.

Revision ID: 6f1a2b3c4d5e
Revises: 15c401f28f5d
Create Date: 2026-08-27 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f1a2b3c4d5e"
down_revision: Union[str, None] = "15c401f28f5d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_credentials",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("plan_tier", sa.String(length=50), nullable=True),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("model_high", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("model_low", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("stale_until", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("priority", name="uq_llm_credentials_priority"),
    )
    op.create_index("ix_llm_credentials_priority", "llm_credentials", ["priority"])

    op.add_column(
        "executions",
        sa.Column("retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("retry_target_url", sa.String(length=2000), nullable=True),
    )
    op.create_index(
        "ix_executions_status_retry_at",
        "executions",
        ["status", "retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_executions_status_retry_at", table_name="executions")
    op.drop_column("executions", "retry_target_url")
    op.drop_column("executions", "retry_at")
    op.drop_index("ix_llm_credentials_priority", table_name="llm_credentials")
    op.drop_table("llm_credentials")
