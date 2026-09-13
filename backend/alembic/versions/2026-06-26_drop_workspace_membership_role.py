"""Drop ``role`` column from workspace_memberships.

Workspace membership no longer carries a role — access is determined solely
by OrgMembership (which still has a role tied to the provider org).

Revision ID: 9f2g3h4i5j6k
Revises: 8e1f2a3b4c5d
Create Date: 2026-06-26 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "9f2g3h4i5j6k"
down_revision: Union[str, None] = "8e1f2a3b4c5d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("workspace_memberships", "role")


def downgrade() -> None:
    op.add_column(
        "workspace_memberships",
        sa.Column("role", sa.String(length=50), nullable=True, server_default="member"),
    )
