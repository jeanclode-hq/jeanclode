"""separate sentry orgs from organizations

Revision ID: a1b2c3d4e5f6
Revises: d98c6d207820
Create Date: 2026-03-18 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "d98c6d207820"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Separate Sentry orgs from organizations."""
    # 1. Create sentry_orgs table
    op.create_table(
        "sentry_orgs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("sentry_org_slug", sa.String(length=255), nullable=False),
        sa.Column("sentry_installation_id", sa.String(length=255), nullable=True),
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
        sa.UniqueConstraint("sentry_org_slug"),
        sa.UniqueConstraint("sentry_installation_id"),
    )

    # 2. Create sentry_org_memberships table
    op.create_table(
        "sentry_org_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("sentry_org_id", sa.Uuid(), nullable=False),
        sa.Column("provider_identity_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
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
        sa.ForeignKeyConstraint(["sentry_org_id"], ["sentry_orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["provider_identity_id"], ["provider_identities.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sentry_org_id", "provider_identity_id", name="uq_sentry_org_membership"
        ),
    )

    # 2b. Migrate organization_memberships: user_id -> provider_identity_id
    op.add_column(
        "organization_memberships",
        sa.Column("provider_identity_id", sa.Uuid(), nullable=True),
    )
    op.execute("""
        UPDATE organization_memberships om
        SET provider_identity_id = (
            SELECT pi.id
            FROM provider_identities pi
            WHERE pi.user_id = om.user_id
            ORDER BY pi.created_at
            LIMIT 1
        )
    """)
    # Remove memberships that couldn't be linked
    op.execute("DELETE FROM organization_memberships WHERE provider_identity_id IS NULL")
    op.alter_column("organization_memberships", "provider_identity_id", nullable=False)
    op.drop_constraint("uq_org_membership", "organization_memberships", type_="unique")
    op.drop_constraint(
        "organization_memberships_user_id_fkey", "organization_memberships", type_="foreignkey"
    )
    op.drop_column("organization_memberships", "user_id")
    op.create_foreign_key(
        "organization_memberships_provider_identity_id_fkey",
        "organization_memberships",
        "provider_identities",
        ["provider_identity_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_org_membership",
        "organization_memberships",
        ["organization_id", "provider_identity_id"],
    )

    # 3. Migrate data: create sentry_orgs from organizations that have sentry_org_slug
    op.execute("""
        INSERT INTO sentry_orgs (id, name, sentry_org_slug, sentry_installation_id, created_at, updated_at)
        SELECT gen_random_uuid(), name, sentry_org_slug, sentry_installation_id, created_at, updated_at
        FROM organizations
        WHERE sentry_org_slug IS NOT NULL
    """)

    # 4. Add sentry_org_id column to sentry_projects
    op.add_column("sentry_projects", sa.Column("sentry_org_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_sentry_projects_sentry_org_id",
        "sentry_projects",
        "sentry_orgs",
        ["sentry_org_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 5. Migrate sentry_projects: link to sentry_orgs via organization's sentry_org_slug
    op.execute("""
        UPDATE sentry_projects sp
        SET sentry_org_id = so.id
        FROM organizations o
        JOIN sentry_orgs so ON so.sentry_org_slug = o.sentry_org_slug
        WHERE sp.organization_id = o.id
    """)

    # 6. Delete orphaned sentry_projects that couldn't be linked to a sentry_org
    op.execute("DELETE FROM sentry_projects WHERE sentry_org_id IS NULL")

    # Drop old FK and column from sentry_projects
    op.drop_constraint("uq_org_sentry_project", "sentry_projects", type_="unique")
    op.drop_constraint(
        "sentry_projects_organization_id_fkey", "sentry_projects", type_="foreignkey"
    )
    op.drop_column("sentry_projects", "organization_id")
    op.alter_column("sentry_projects", "sentry_org_id", nullable=False)
    op.create_unique_constraint(
        "uq_sentry_org_project", "sentry_projects", ["sentry_org_id", "sentry_project_id"]
    )

    # 8. Update issues: drop organization_id, update constraints
    op.drop_constraint("uq_org_sentry_issue", "issues", type_="unique")
    op.drop_index("ix_issues_organization_id", table_name="issues")
    op.drop_constraint("issues_organization_id_fkey", "issues", type_="foreignkey")
    op.drop_column("issues", "organization_id")
    op.create_unique_constraint(
        "uq_sentry_project_issue", "issues", ["sentry_project_id", "sentry_issue_id"]
    )
    op.create_index("ix_issues_sentry_project_id", "issues", ["sentry_project_id"], unique=False)

    # 7. Drop sentry_auth_token_encrypted from sentry_projects (existed in initial migration)
    op.drop_column("sentry_projects", "sentry_auth_token_encrypted")

    # 8. Drop sentry fields from organizations
    op.drop_constraint("organizations_sentry_installation_id_key", "organizations", type_="unique")
    op.drop_column("organizations", "sentry_org_slug")
    op.drop_column("organizations", "sentry_installation_id")


def downgrade() -> None:
    """Reverse the sentry org separation."""
    # Re-add sentry fields to organizations
    op.add_column(
        "organizations",
        sa.Column("sentry_org_slug", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("sentry_installation_id", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint(
        "organizations_sentry_installation_id_key", "organizations", ["sentry_installation_id"]
    )

    # Re-add organization_id to issues
    op.add_column("issues", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.drop_index("ix_issues_sentry_project_id", table_name="issues")
    op.drop_constraint("uq_sentry_project_issue", "issues", type_="unique")
    op.create_foreign_key(
        "issues_organization_id_fkey",
        "issues",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_issues_organization_id", "issues", ["organization_id"], unique=False)
    op.create_unique_constraint(
        "uq_org_sentry_issue", "issues", ["organization_id", "sentry_issue_id"]
    )

    # Re-add sentry_auth_token_encrypted to sentry_projects
    op.add_column(
        "sentry_projects",
        sa.Column("sentry_auth_token_encrypted", sa.String(), nullable=True),
    )

    # Re-add organization_id to sentry_projects
    op.add_column("sentry_projects", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.drop_constraint("uq_sentry_org_project", "sentry_projects", type_="unique")
    op.drop_constraint("fk_sentry_projects_sentry_org_id", "sentry_projects", type_="foreignkey")
    op.drop_column("sentry_projects", "sentry_org_id")
    op.create_foreign_key(
        "sentry_projects_organization_id_fkey",
        "sentry_projects",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_org_sentry_project", "sentry_projects", ["organization_id", "sentry_project_id"]
    )

    # Revert organization_memberships: provider_identity_id -> user_id
    op.add_column(
        "organization_memberships",
        sa.Column("user_id", sa.Uuid(), nullable=True),
    )
    op.execute("""
        UPDATE organization_memberships om
        SET user_id = pi.user_id
        FROM provider_identities pi
        WHERE pi.id = om.provider_identity_id
    """)
    op.drop_constraint("uq_org_membership", "organization_memberships", type_="unique")
    op.drop_constraint(
        "organization_memberships_provider_identity_id_fkey",
        "organization_memberships",
        type_="foreignkey",
    )
    op.drop_column("organization_memberships", "provider_identity_id")
    op.alter_column("organization_memberships", "user_id", nullable=False)
    op.create_foreign_key(
        "organization_memberships_user_id_fkey",
        "organization_memberships",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_org_membership", "organization_memberships", ["organization_id", "user_id"]
    )

    # Drop new tables
    op.drop_table("sentry_org_memberships")
    op.drop_table("sentry_orgs")
