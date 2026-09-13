"""sentry org nullable workspace

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-26 12:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make sentry_orgs.workspace_id nullable for webhook-first onboarding."""
    op.alter_column("sentry_orgs", "workspace_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    """Restore NOT NULL constraint on sentry_orgs.workspace_id."""
    op.alter_column("sentry_orgs", "workspace_id", existing_type=sa.Uuid(), nullable=False)
