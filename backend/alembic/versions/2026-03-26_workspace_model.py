"""workspace model

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-26 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add workspace model and rename organizations to git_orgs."""
    # 1. Create workspaces table
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # 2. Create workspace_memberships table
    op.create_table(
        "workspace_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=50), server_default="member", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
    )

    # 3. Rename organizations -> git_orgs
    op.rename_table("organizations", "git_orgs")

    # 4. Rename organization_memberships -> git_org_memberships
    op.rename_table("organization_memberships", "git_org_memberships")

    # 5. Add workspace_id column to git_orgs (NOT NULL — no existing data in prod)
    op.add_column("git_orgs", sa.Column("workspace_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "fk_git_orgs_workspace_id",
        "git_orgs",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 6. Add workspace_id column to sentry_orgs (NOT NULL — no existing data in prod)
    op.add_column("sentry_orgs", sa.Column("workspace_id", sa.Uuid(), nullable=False))
    op.create_foreign_key(
        "fk_sentry_orgs_workspace_id",
        "sentry_orgs",
        "workspaces",
        ["workspace_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 7. Rename organization_id -> git_org_id in repositories
    #    First drop the old FK, rename column, then create new FK
    op.drop_constraint("repositories_organization_id_fkey", "repositories", type_="foreignkey")
    op.alter_column("repositories", "organization_id", new_column_name="git_org_id")
    op.create_foreign_key(
        "repositories_git_org_id_fkey",
        "repositories",
        "git_orgs",
        ["git_org_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 8. Rename organization_id -> git_org_id in git_org_memberships
    #    First drop the old FK and unique constraint, rename column, then recreate
    op.drop_constraint("uq_org_membership", "git_org_memberships", type_="unique")
    op.drop_constraint(
        "organization_memberships_organization_id_fkey",
        "git_org_memberships",
        type_="foreignkey",
    )
    op.alter_column("git_org_memberships", "organization_id", new_column_name="git_org_id")
    op.create_foreign_key(
        "git_org_memberships_git_org_id_fkey",
        "git_org_memberships",
        "git_orgs",
        ["git_org_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 9. Rename constraint uq_org_membership -> uq_git_org_membership
    op.create_unique_constraint(
        "uq_git_org_membership",
        "git_org_memberships",
        ["git_org_id", "provider_identity_id"],
    )


def downgrade() -> None:
    """Reverse workspace model and rename git_orgs back to organizations."""
    # 9. Drop uq_git_org_membership, will be recreated as uq_org_membership
    op.drop_constraint("uq_git_org_membership", "git_org_memberships", type_="unique")

    # 8. Rename git_org_id -> organization_id in git_org_memberships
    op.drop_constraint(
        "git_org_memberships_git_org_id_fkey",
        "git_org_memberships",
        type_="foreignkey",
    )
    op.alter_column("git_org_memberships", "git_org_id", new_column_name="organization_id")
    op.create_foreign_key(
        "organization_memberships_organization_id_fkey",
        "git_org_memberships",
        "git_orgs",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_org_membership",
        "git_org_memberships",
        ["organization_id", "provider_identity_id"],
    )

    # 7. Rename git_org_id -> organization_id in repositories
    op.drop_constraint("repositories_git_org_id_fkey", "repositories", type_="foreignkey")
    op.alter_column("repositories", "git_org_id", new_column_name="organization_id")
    op.create_foreign_key(
        "repositories_organization_id_fkey",
        "repositories",
        "git_orgs",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 6. Drop workspace_id from sentry_orgs
    op.drop_constraint("fk_sentry_orgs_workspace_id", "sentry_orgs", type_="foreignkey")
    op.drop_column("sentry_orgs", "workspace_id")

    # 5. Drop workspace_id from git_orgs
    op.drop_constraint("fk_git_orgs_workspace_id", "git_orgs", type_="foreignkey")
    op.drop_column("git_orgs", "workspace_id")

    # 4. Rename git_org_memberships -> organization_memberships
    op.rename_table("git_org_memberships", "organization_memberships")

    # 3. Rename git_orgs -> organizations
    op.rename_table("git_orgs", "organizations")

    # 2. Drop workspace_memberships table
    op.drop_table("workspace_memberships")

    # 1. Drop workspaces table
    op.drop_table("workspaces")
