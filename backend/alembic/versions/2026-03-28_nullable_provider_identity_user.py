"""fix: allow nullable user_id on provider_identities

The initial migration created user_id as NOT NULL with CASCADE delete,
but the model defines it as nullable with SET NULL. Provider identities
can exist without a user (e.g. org member sync before the member logs in).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-28 10:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make user_id nullable and fix ondelete to SET NULL."""
    op.alter_column("provider_identities", "user_id", existing_type=sa.Uuid(), nullable=True)
    op.drop_constraint(
        "provider_identities_user_id_fkey", "provider_identities", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_provider_identities_user_id",
        "provider_identities",
        "users",
        ["user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Restore NOT NULL and CASCADE."""
    op.drop_constraint("fk_provider_identities_user_id", "provider_identities", type_="foreignkey")
    op.create_foreign_key(
        "provider_identities_user_id_fkey",
        "provider_identities",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("provider_identities", "user_id", existing_type=sa.Uuid(), nullable=False)
