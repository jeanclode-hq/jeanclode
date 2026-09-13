"""Drop current_step column from executions.

Revision ID: c3d4e5f6a7b8
Revises: f7e8d9c0b1a2
Create Date: 2026-04-20 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a9f1b2c4d5e"
down_revision: Union[str, None] = "f7e8d9c0b1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("executions", "current_step")


def downgrade() -> None:
    op.add_column("executions", sa.Column("current_step", sa.String(50), nullable=True))
