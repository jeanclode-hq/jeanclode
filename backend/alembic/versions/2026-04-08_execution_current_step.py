"""Add current_step to executions for pipeline progress tracking.

Revision ID: 9b3c5d7e1f4a
Revises: 7a8b9c0d1e2f
Create Date: 2026-04-08 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3c5d7e1f4a"
down_revision: Union[str, None] = "8b9c0d1e2f3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("current_step", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "current_step")
