"""Merge git_orgs + sentry_orgs into unified organizations table.

Merges GitOrg and SentryOrg into a single Organization model,
GitOrgMembership and SentryOrgMembership into OrgMembership,
and renames sentry_projects to source_projects.

Revision ID: 7a8b9c0d1e2f
Revises: 6df6c4e22418
Create Date: 2026-04-07 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a8b9c0d1e2f"
down_revision: Union[str, Sequence[str], None] = "6df6c4e22418"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create unified organizations, org_memberships, source_projects tables and migrate data."""

    # 1. Create new tables
    op.create_table(
        "organizations",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("external_org_id", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.String(255), nullable=True, unique=True),
        sa.Column("base_url", sa.String(500), nullable=True),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("auth_token_encrypted", sa.Text(), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onboarding_step", sa.String(50), nullable=False, server_default="git_provider"),
        sa.Column("settings", JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "provider", "external_org_id", "base_url", name="uq_org_provider_external"
        ),
    )

    op.create_table(
        "org_memberships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "org_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_identity_id",
            sa.UUID(),
            sa.ForeignKey("provider_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(50), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "provider_identity_id", name="uq_org_membership"),
    )

    op.create_table(
        "source_projects",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "org_id",
            sa.UUID(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("external_project_id", sa.String(255), nullable=False),
        sa.Column(
            "repo_id",
            sa.UUID(),
            sa.ForeignKey("repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mapping_source", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "external_project_id", name="uq_source_project_org_external"),
    )

    # 2. Copy data from git_orgs -> organizations (preserving UUIDs)
    op.execute("""
        INSERT INTO organizations (id, workspace_id, name, provider, external_org_id,
            installation_id, base_url, avatar_url, auth_token_encrypted,
            onboarding_step, settings, created_at, updated_at)
        SELECT id, workspace_id, name, provider, external_org_id,
            installation_id, provider_url, avatar_url, gitlab_access_token_encrypted,
            onboarding_step, settings, created_at, updated_at
        FROM git_orgs
    """)

    # 3. Copy data from sentry_orgs -> organizations (preserving UUIDs)
    op.execute("""
        INSERT INTO organizations (id, workspace_id, name, provider, external_org_id,
            installation_id, base_url, avatar_url, auth_token_encrypted,
            client_secret_encrypted, last_dispatched_at,
            onboarding_step, settings, created_at, updated_at)
        SELECT id, workspace_id, name, 'sentry', sentry_org_slug,
            sentry_installation_id, base_url, NULL, auth_token_encrypted,
            client_secret_encrypted, last_dispatched_at,
            'complete', settings, created_at, updated_at
        FROM sentry_orgs
    """)

    # 4. Copy memberships
    op.execute("""
        INSERT INTO org_memberships (id, org_id, provider_identity_id, role, created_at, updated_at)
        SELECT id, git_org_id, provider_identity_id, role, created_at, updated_at
        FROM git_org_memberships
    """)
    op.execute("""
        INSERT INTO org_memberships (id, org_id, provider_identity_id, role, created_at, updated_at)
        SELECT id, sentry_org_id, provider_identity_id, role, created_at, updated_at
        FROM sentry_org_memberships
    """)

    # 5. Copy source projects (preserving UUIDs so issues FK stays valid)
    op.execute("""
        INSERT INTO source_projects (id, org_id, slug, external_project_id,
            repo_id, mapping_source, created_at, updated_at)
        SELECT id, sentry_org_id, sentry_project_slug, sentry_project_id,
            repo_id, mapping_source, created_at, updated_at
        FROM sentry_projects
    """)

    # 6. Add org_id column to repositories and populate from git_org_id
    op.add_column("repositories", sa.Column("org_id", sa.UUID(), nullable=True))
    op.execute("UPDATE repositories SET org_id = git_org_id")
    op.alter_column("repositories", "org_id", nullable=False)
    op.create_foreign_key(
        "fk_repositories_org_id",
        "repositories",
        "organizations",
        ["org_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 7. Update issues FK to point at source_projects
    op.drop_constraint("issues_sentry_project_id_fkey", "issues", type_="foreignkey")
    op.create_foreign_key(
        "fk_issues_source_project_id",
        "issues",
        "source_projects",
        ["source_project_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 8. Drop old FK from repositories
    op.drop_constraint("repositories_git_org_id_fkey", "repositories", type_="foreignkey")
    op.drop_column("repositories", "git_org_id")

    # 9. Drop old tables (order matters for FKs)
    op.drop_table("sentry_projects")
    op.drop_table("git_org_memberships")
    op.drop_table("sentry_org_memberships")
    op.drop_table("git_orgs")
    op.drop_table("sentry_orgs")


def downgrade() -> None:
    """Reverse the unified organization migration."""
    # Recreate old tables
    op.create_table(
        "git_orgs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("external_org_id", sa.String(255), nullable=False),
        sa.Column("installation_id", sa.String(255), nullable=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_url", sa.String(500), nullable=True),
        sa.Column("avatar_url", sa.String(500), nullable=True),
        sa.Column("gitlab_access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("onboarding_step", sa.String(50), nullable=False, server_default="git_provider"),
        sa.Column("settings", JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "sentry_orgs",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("sentry_org_slug", sa.String(255), nullable=False),
        sa.Column("sentry_installation_id", sa.String(255), nullable=True, unique=True),
        sa.Column("base_url", sa.String(500), nullable=False, server_default="https://sentry.io"),
        sa.Column("auth_token_encrypted", sa.Text(), nullable=True),
        sa.Column("client_secret_encrypted", sa.Text(), nullable=True),
        sa.Column("last_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settings", JSONB, server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("sentry_org_slug", "base_url", name="uq_sentry_org_slug_base_url"),
    )

    op.create_table(
        "git_org_memberships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "git_org_id",
            sa.UUID(),
            sa.ForeignKey("git_orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_identity_id",
            sa.UUID(),
            sa.ForeignKey("provider_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(50), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("git_org_id", "provider_identity_id", name="uq_git_org_membership"),
    )

    op.create_table(
        "sentry_org_memberships",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "sentry_org_id",
            sa.UUID(),
            sa.ForeignKey("sentry_orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "provider_identity_id",
            sa.UUID(),
            sa.ForeignKey("provider_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(50), nullable=False, server_default="member"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "sentry_org_id", "provider_identity_id", name="uq_sentry_org_membership"
        ),
    )

    op.create_table(
        "sentry_projects",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "sentry_org_id",
            sa.UUID(),
            sa.ForeignKey("sentry_orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sentry_project_slug", sa.String(255), nullable=False),
        sa.Column("sentry_project_id", sa.String(255), nullable=False),
        sa.Column(
            "repo_id",
            sa.UUID(),
            sa.ForeignKey("repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("mapping_source", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("sentry_org_id", "sentry_project_id", name="uq_sentry_org_project"),
    )

    # Restore git_org_id on repositories
    op.add_column("repositories", sa.Column("git_org_id", sa.UUID(), nullable=True))
    op.execute("""
        UPDATE repositories SET git_org_id = org_id
    """)
    op.alter_column("repositories", "git_org_id", nullable=False)
    op.create_foreign_key(
        "repositories_git_org_id_fkey",
        "repositories",
        "git_orgs",
        ["git_org_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Copy data back
    op.execute("""
        INSERT INTO git_orgs (id, workspace_id, name, external_org_id, installation_id,
            provider, provider_url, avatar_url, gitlab_access_token_encrypted,
            onboarding_step, settings, created_at, updated_at)
        SELECT id, workspace_id, name, external_org_id, installation_id,
            provider, base_url, avatar_url, auth_token_encrypted,
            onboarding_step, settings, created_at, updated_at
        FROM organizations WHERE provider IN ('github', 'gitlab')
    """)

    op.execute("""
        INSERT INTO sentry_orgs (id, workspace_id, name, sentry_org_slug, sentry_installation_id,
            base_url, auth_token_encrypted, client_secret_encrypted, last_dispatched_at,
            settings, created_at, updated_at)
        SELECT id, workspace_id, name, external_org_id, installation_id,
            COALESCE(base_url, 'https://sentry.io'), auth_token_encrypted, client_secret_encrypted,
            last_dispatched_at, settings, created_at, updated_at
        FROM organizations WHERE provider = 'sentry'
    """)

    op.execute("""
        INSERT INTO git_org_memberships (id, git_org_id, provider_identity_id, role, created_at, updated_at)
        SELECT om.id, om.org_id, om.provider_identity_id, om.role, om.created_at, om.updated_at
        FROM org_memberships om
        JOIN organizations o ON o.id = om.org_id
        WHERE o.provider IN ('github', 'gitlab')
    """)

    op.execute("""
        INSERT INTO sentry_org_memberships (id, sentry_org_id, provider_identity_id, role, created_at, updated_at)
        SELECT om.id, om.org_id, om.provider_identity_id, om.role, om.created_at, om.updated_at
        FROM org_memberships om
        JOIN organizations o ON o.id = om.org_id
        WHERE o.provider = 'sentry'
    """)

    op.execute("""
        INSERT INTO sentry_projects (id, sentry_org_id, sentry_project_slug, sentry_project_id,
            repo_id, mapping_source, created_at, updated_at)
        SELECT id, org_id, slug, external_project_id, repo_id, mapping_source, created_at, updated_at
        FROM source_projects
    """)

    # Restore issues FK
    op.drop_constraint("fk_issues_source_project_id", "issues", type_="foreignkey")
    op.create_foreign_key(
        "issues_sentry_project_id_fkey",
        "issues",
        "sentry_projects",
        ["source_project_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Drop repositories.org_id
    op.drop_constraint("fk_repositories_org_id", "repositories", type_="foreignkey")
    op.drop_column("repositories", "org_id")

    # Drop new tables
    op.drop_table("source_projects")
    op.drop_table("org_memberships")
    op.drop_table("organizations")
