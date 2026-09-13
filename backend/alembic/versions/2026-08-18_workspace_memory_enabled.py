"""add memory_enabled flag to workspaces

Per-workspace opt-in gate for the container memory wiring (#181) —
dispatch call sites only mint a memory API token and add its upstream
credential (see api.plugins.container.dispatch_inputs.add_memory_to_inputs)
when the owning workspace's ``memory_enabled`` is True.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add workspaces.memory_enabled, defaulting existing rows to False."""
    op.add_column(
        "workspaces",
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop workspaces.memory_enabled."""
    op.drop_column("workspaces", "memory_enabled")
