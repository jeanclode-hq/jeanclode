"""llm credential name and heavy model

Revision ID: 4b0f2d8e6c3a
Revises: 67e90faaeb78
Create Date: 2026-09-25 12:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4b0f2d8e6c3a"
down_revision: Union[str, Sequence[str], None] = "67e90faaeb78"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_credentials",
        sa.Column("name", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "llm_credentials",
        sa.Column("model_heavy", sa.String(length=255), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("llm_credentials", "model_heavy")
    op.drop_column("llm_credentials", "name")
