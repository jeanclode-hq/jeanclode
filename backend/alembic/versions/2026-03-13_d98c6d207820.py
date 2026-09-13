"""drop sentry_auth_token_encrypted from organizations

Revision ID: d98c6d207820
Revises: e79f4a5319bd
Create Date: 2026-03-13 20:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d98c6d207820"
down_revision: Union[str, Sequence[str], None] = "e79f4a5319bd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop sentry_auth_token_encrypted from organizations."""
    op.drop_column("organizations", "sentry_auth_token_encrypted")


def downgrade() -> None:
    """Re-add sentry_auth_token_encrypted to organizations."""
    op.add_column(
        "organizations",
        sa.Column("sentry_auth_token_encrypted", sa.String(), nullable=True),
    )
