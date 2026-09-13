"""add settings JSONB column to git_orgs, repositories, sentry_orgs

Revision ID: a1b2c3d4e5f6
Revises: 6ac64bc275b5
Create Date: 2026-04-06 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6df6c4e22418"
down_revision: Union[str, Sequence[str], None] = "6ac64bc275b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add settings JSONB column to integration tables."""
    op.add_column("git_orgs", sa.Column("settings", JSONB, server_default="{}", nullable=False))
    op.add_column("repositories", sa.Column("settings", JSONB, server_default="{}", nullable=False))
    op.add_column("sentry_orgs", sa.Column("settings", JSONB, server_default="{}", nullable=False))


def downgrade() -> None:
    """Remove settings columns."""
    op.drop_column("sentry_orgs", "settings")
    op.drop_column("repositories", "settings")
    op.drop_column("git_orgs", "settings")
