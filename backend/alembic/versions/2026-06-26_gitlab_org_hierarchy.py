"""Add parent_org_id and root_org_id to organizations for GitLab hierarchy.

Revision ID: b1c2d3e4f5a6
Revises: 9f2g3h4i5j6k
Create Date: 2026-06-26 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "9f2g3h4i5j6k"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "parent_org_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "root_org_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "root_org_id")
    op.drop_column("organizations", "parent_org_id")
