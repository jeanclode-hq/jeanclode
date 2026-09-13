"""drop workspaces.memory_enabled

Memory is no longer opt-in per workspace — it's always wired into dispatch
for every org with a workspace. See
api.plugins.container.dispatch_inputs.resolve_memory_workspace_id.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-19 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop workspaces.memory_enabled."""
    op.drop_column("workspaces", "memory_enabled")


def downgrade() -> None:
    """Re-add workspaces.memory_enabled, defaulting existing rows to False."""
    op.add_column(
        "workspaces",
        sa.Column("memory_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
