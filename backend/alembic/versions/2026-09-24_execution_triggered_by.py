"""add executions.triggered_by_identity_id

Who @-mentioned the bot, for the dashboard's top-users card. Best effort:
set only when the sender already has a synced provider identity.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-24 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add executions.triggered_by_identity_id."""
    op.add_column(
        "executions",
        sa.Column(
            "triggered_by_identity_id",
            sa.UUID(),
            sa.ForeignKey("provider_identities.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_executions_triggered_by_identity_id",
        "executions",
        ["triggered_by_identity_id"],
    )


def downgrade() -> None:
    """Drop executions.triggered_by_identity_id."""
    op.drop_index("ix_executions_triggered_by_identity_id", table_name="executions")
    op.drop_column("executions", "triggered_by_identity_id")
