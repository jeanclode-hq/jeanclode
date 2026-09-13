"""Move repo-to-repo mapping into its own table.

``repositories.mapped_repo_id``/``mapping_method`` only supported one
outgoing edge per row, which works for the Sentry-project-to-git-repo case
but not for grouping several git repos together (a repo needs many outgoing
edges there). Moves both columns into a new ``repository_mappings`` table
keyed by (repo_id, mapped_repo_id), and backfills existing data.

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-07-27 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create repository_mappings, backfill from repositories, drop old columns."""

    op.create_table(
        "repository_mappings",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "repo_id",
            sa.UUID(),
            sa.ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mapped_repo_id",
            sa.UUID(),
            sa.ForeignKey("repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mapping_method", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("repo_id", "mapped_repo_id", name="uq_repository_mapping_pair"),
    )
    op.create_index("ix_repository_mappings_repo_id", "repository_mappings", ["repo_id"])
    op.create_index(
        "ix_repository_mappings_mapped_repo_id", "repository_mappings", ["mapped_repo_id"]
    )

    op.execute("""
        INSERT INTO repository_mappings (id, repo_id, mapped_repo_id, mapping_method,
            created_at, updated_at)
        SELECT gen_random_uuid(), id, mapped_repo_id, mapping_method, created_at, updated_at
        FROM repositories
        WHERE mapped_repo_id IS NOT NULL OR mapping_method IS NOT NULL
    """)

    op.drop_column("repositories", "mapped_repo_id")
    op.drop_column("repositories", "mapping_method")


def downgrade() -> None:
    """Reverse: recreate the columns on repositories and drop the table."""

    op.add_column(
        "repositories",
        sa.Column(
            "mapped_repo_id",
            sa.UUID(),
            sa.ForeignKey("repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "repositories",
        sa.Column("mapping_method", sa.String(50), nullable=True),
    )

    # Cross-source rows only — a repo with multiple repository_mappings rows
    # (git-to-git groups) can't be losslessly folded back into a single column,
    # so downgrade keeps the first mapping per repo_id and drops the rest.
    op.execute("""
        UPDATE repositories r
        SET mapped_repo_id = rm.mapped_repo_id,
            mapping_method = rm.mapping_method
        FROM (
            SELECT DISTINCT ON (repo_id) repo_id, mapped_repo_id, mapping_method
            FROM repository_mappings
            ORDER BY repo_id, created_at
        ) rm
        WHERE r.id = rm.repo_id
    """)

    op.drop_table("repository_mappings")
