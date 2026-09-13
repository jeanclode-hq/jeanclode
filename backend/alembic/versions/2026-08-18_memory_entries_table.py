"""add memory_entries table

Backs the agent memory tool (Anthropic ``memory_20250818`` contract) as
flat, workspace-scoped key-value storage. The unique constraint on
``(workspace_id, path)`` is the concurrency guard for the ``create``
command — see ``api.models.memory.MemoryEntry``.

Revision ID: d5e6f7a8b9c0
Revises: c2d3e4f5a6b7
Create Date: 2026-08-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create memory_entries with a (workspace_id, path) unique constraint."""

    op.create_table(
        "memory_entries",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("metadata_json", JSONB, nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "path", name="uq_memory_entry_path"),
    )
    op.create_index("ix_memory_entries_workspace_id", "memory_entries", ["workspace_id"])


def downgrade() -> None:
    """Drop memory_entries."""
    op.drop_index("ix_memory_entries_workspace_id", table_name="memory_entries")
    op.drop_table("memory_entries")
