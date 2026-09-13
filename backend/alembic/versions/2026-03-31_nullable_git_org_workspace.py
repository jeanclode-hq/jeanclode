"""make git_orgs.workspace_id nullable

Revision ID: 53625f8b2e49
Revises: 2f89cc7261c1
Create Date: 2026-03-31 01:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "53625f8b2e49"
down_revision: Union[str, Sequence[str], None] = "2f89cc7261c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make workspace_id nullable on git_orgs for orphaned installs."""
    op.alter_column("git_orgs", "workspace_id", nullable=True)


def downgrade() -> None:
    """Revert workspace_id to non-nullable."""
    op.alter_column("git_orgs", "workspace_id", nullable=False)
