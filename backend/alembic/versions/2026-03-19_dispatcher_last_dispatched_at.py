"""move last_dispatched_at from organizations to sentry_orgs

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-19 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Move last_dispatched_at from organizations to sentry_orgs."""
    op.add_column(
        "sentry_orgs",
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column("organizations", "last_dispatched_at")


def downgrade() -> None:
    """Move last_dispatched_at back to organizations."""
    op.add_column(
        "organizations",
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column("sentry_orgs", "last_dispatched_at")
