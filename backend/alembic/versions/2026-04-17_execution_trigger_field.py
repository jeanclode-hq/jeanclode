"""Add trigger field to executions for manual vs auto dispatch.

Revision ID: a1b2c3d4e5f6
Revises: 9b3c5d7e1f4a
Create Date: 2026-04-17 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7e8d9c0b1a2"
down_revision: Union[str, None] = "9b3c5d7e1f4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column("trigger", sa.String(50), nullable=False, server_default="auto"),
    )


def downgrade() -> None:
    op.drop_column("executions", "trigger")
