"""add per-org sentry credentials to sentry_orgs

Revision ID: 74f8eac09344
Revises: 53625f8b2e49
Create Date: 2026-03-31 02:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "74f8eac09344"
down_revision: Union[str, Sequence[str], None] = "53625f8b2e49"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add per-org credentials to sentry_orgs."""
    op.add_column(
        "sentry_orgs",
        sa.Column("base_url", sa.String(500), nullable=False, server_default="https://sentry.io"),
    )
    op.add_column("sentry_orgs", sa.Column("auth_token_encrypted", sa.Text(), nullable=True))
    op.add_column("sentry_orgs", sa.Column("client_secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove per-org credentials."""
    op.drop_column("sentry_orgs", "client_secret_encrypted")
    op.drop_column("sentry_orgs", "auth_token_encrypted")
    op.drop_column("sentry_orgs", "base_url")
