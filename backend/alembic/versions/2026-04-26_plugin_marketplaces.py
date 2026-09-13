"""Add plugin_marketplaces and plugin_installations tables.

Revision ID: 6c9d0e1f2a3b
Revises: 5e7f8a9b0c1d
Create Date: 2026-04-26 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6c9d0e1f2a3b"
down_revision: Union[str, None] = "5e7f8a9b0c1d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "plugin_marketplaces",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("git_url", sa.String(length=500), nullable=False),
        sa.Column("last_sync_status", sa.String(length=20), nullable=False, server_default="ok"),
        sa.Column("last_sync_error", sa.String(length=2000), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "git_url", name="uq_plugin_marketplace_org_url"),
    )

    op.create_table(
        "plugin_installations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("marketplace_id", sa.UUID(), nullable=False),
        sa.Column("plugin_name", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
        sa.Column("pinned_ref", sa.String(length=255), nullable=True),
        sa.Column("enabled_workflows", postgresql.JSONB(), nullable=True),
        sa.Column("project_overrides", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["marketplace_id"], ["plugin_marketplaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "marketplace_id", "plugin_name", name="uq_plugin_install_marketplace"
        ),
    )


def downgrade() -> None:
    op.drop_table("plugin_installations")
    op.drop_table("plugin_marketplaces")
