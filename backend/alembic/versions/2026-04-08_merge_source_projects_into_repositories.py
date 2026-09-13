"""Merge source_projects into repositories table.

SourceProject is eliminated — Repository becomes the single child of
Organization. Issues and PRs both FK to Repository. Repo-to-repo links
via mapped_repo_id handle the sentry→git mapping.

Revision ID: 8b9c0d1e2f3a
Revises: 7a8b9c0d1e2f
Create Date: 2026-04-08 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b9c0d1e2f3a"
down_revision: Union[str, Sequence[str], None] = "7a8b9c0d1e2f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge source_projects into repositories."""

    # 1. Rename external_repo_id → external_id on repositories
    op.alter_column("repositories", "external_repo_id", new_column_name="external_id")

    # 2. Rename gitlab_access_token_encrypted → auth_token_encrypted
    op.alter_column(
        "repositories", "gitlab_access_token_encrypted", new_column_name="auth_token_encrypted"
    )

    # 3. Add new columns to repositories
    op.add_column(
        "repositories",
        sa.Column(
            "mapped_repo_id",
            sa.UUID(),
            sa.ForeignKey("repositories.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "repositories",
        sa.Column("mapping_method", sa.String(50), nullable=True),
    )

    # 4. Add unique constraint on (org_id, external_id)
    op.create_unique_constraint(
        "uq_repository_org_external", "repositories", ["org_id", "external_id"]
    )

    # 5. Copy source_projects into repositories (preserving UUIDs)
    op.execute("""
        INSERT INTO repositories (id, org_id, external_id, name, provider, mapped_repo_id,
            mapping_method, created_at, updated_at)
        SELECT sp.id, sp.org_id, sp.external_project_id, sp.slug, 'sentry', sp.repo_id,
            sp.mapping_source, sp.created_at, sp.updated_at
        FROM source_projects sp
    """)

    # 6. Update issues FK: source_project_id → repository_id
    op.alter_column("issues", "source_project_id", new_column_name="repository_id")

    # Update FK constraint
    op.drop_constraint("fk_issues_source_project_id", "issues", type_="foreignkey")
    op.create_foreign_key(
        "fk_issues_repository_id",
        "issues",
        "repositories",
        ["repository_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Update indexes
    op.drop_index("ix_issues_source_project_id", table_name="issues")
    op.create_index("ix_issues_repository_id", "issues", ["repository_id"])

    # Update unique constraint
    op.drop_constraint("uq_issue_source_project_external", "issues", type_="unique")
    op.create_unique_constraint(
        "uq_issue_repository_external", "issues", ["repository_id", "external_id"]
    )

    # 7. Drop source_projects table
    op.drop_table("source_projects")


def downgrade() -> None:
    """Reverse: recreate source_projects from repositories."""

    # 1. Recreate source_projects table
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

    # 2. Copy sentry repos back to source_projects
    op.execute("""
        INSERT INTO source_projects (id, org_id, slug, external_project_id, repo_id,
            mapping_source, created_at, updated_at)
        SELECT r.id, r.org_id, r.name, r.external_id, r.mapped_repo_id,
            r.mapping_method, r.created_at, r.updated_at
        FROM repositories r
        WHERE r.provider = 'sentry'
    """)

    # 3. Revert issues FK
    op.alter_column("issues", "repository_id", new_column_name="source_project_id")
    op.drop_constraint("fk_issues_repository_id", "issues", type_="foreignkey")
    op.create_foreign_key(
        "fk_issues_source_project_id",
        "issues",
        "source_projects",
        ["source_project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_index("ix_issues_repository_id", table_name="issues")
    op.create_index("ix_issues_source_project_id", "issues", ["source_project_id"])
    op.drop_constraint("uq_issue_repository_external", "issues", type_="unique")
    op.create_unique_constraint(
        "uq_issue_source_project_external", "issues", ["source_project_id", "external_id"]
    )

    # 4. Delete sentry repos from repositories
    op.execute("DELETE FROM repositories WHERE provider = 'sentry'")

    # 5. Remove new columns and revert renames
    op.drop_constraint("uq_repository_org_external", "repositories", type_="unique")
    op.drop_column("repositories", "mapping_method")
    op.drop_column("repositories", "mapped_repo_id")
    op.alter_column(
        "repositories", "auth_token_encrypted", new_column_name="gitlab_access_token_encrypted"
    )
    op.alter_column("repositories", "external_id", new_column_name="external_repo_id")
