"""execution fixer llm

Revision ID: 5c1a3e9f7b2d
Revises: 4b0f2d8e6c3a
Create Date: 2026-09-25 18:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5c1a3e9f7b2d"
down_revision: Union[str, Sequence[str], None] = "4b0f2d8e6c3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("fixer_llm_credential", sa.String(100), nullable=True))
    op.add_column("executions", sa.Column("fixer_llm_model", sa.String(255), nullable=True))
    op.add_column("executions", sa.Column("fixer_llm_reason", sa.String(2000), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "fixer_llm_reason")
    op.drop_column("executions", "fixer_llm_model")
    op.drop_column("executions", "fixer_llm_credential")
