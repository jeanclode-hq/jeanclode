"""memory soft delete and curation

Revision ID: 8d4e2a7c1f90
Revises: 5c1a3e9f7b2d
Create Date: 2026-09-26 12:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d4e2a7c1f90"
down_revision: Union[str, Sequence[str], None] = "5c1a3e9f7b2d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_entries", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "memory_entries", sa.Column("curated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "workspaces", sa.Column("memory_curated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.drop_constraint("uq_memory_entry_path", "memory_entries", type_="unique")
    op.create_index(
        "uq_memory_entry_live_path",
        "memory_entries",
        ["workspace_id", "path"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM memory_entries WHERE deleted_at IS NOT NULL")
    op.drop_index("uq_memory_entry_live_path", table_name="memory_entries")
    op.create_unique_constraint("uq_memory_entry_path", "memory_entries", ["workspace_id", "path"])
    op.drop_column("workspaces", "memory_curated_at")
    op.drop_column("memory_entries", "curated_at")
    op.drop_column("memory_entries", "deleted_at")
