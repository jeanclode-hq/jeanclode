"""Add instance_settings table for encrypted key-value instance config.

Revision ID: 5e7f8a9b0c1d
Revises: 3a9f1b2c4d5e
Create Date: 2026-04-20 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e7f8a9b0c1d"
down_revision: Union[str, None] = "3a9f1b2c4d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "instance_settings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category", "key", name="uq_instance_settings_category_key"),
    )
    op.create_index(
        "ix_instance_settings_category",
        "instance_settings",
        ["category"],
    )


def downgrade() -> None:
    op.drop_index("ix_instance_settings_category", table_name="instance_settings")
    op.drop_table("instance_settings")
