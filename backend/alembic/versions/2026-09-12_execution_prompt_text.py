"""add executions.prompt_text

Surfaces the @jeanclode-bot mention/comment body that triggered a RESPOND
execution in the dashboard detail modal. Nothing persisted it before — only
the comment URL (retry_target_url), which the CLI re-fetches at run time.

Revision ID: b3c4d5e6f7a8
Revises: 972714edb971
Create Date: 2026-09-12 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "972714edb971"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add executions.prompt_text."""
    op.add_column("executions", sa.Column("prompt_text", sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop executions.prompt_text."""
    op.drop_column("executions", "prompt_text")
