"""Cascade organization deletes down the GitLab group hierarchy.

Subgroup orgs exist only to hold the projects of the group that was
connected. ``ON DELETE SET NULL`` left them behind when their group was
deleted, orphaned at the top of the tree — where nothing distinguishes them
from a group someone connected on purpose, so one deleted connection came
back as dozens of phantom ones.

Revision ID: a4d7e2c91b83
Revises: 6f1a2b3c4d5e
Create Date: 2026-08-31 00:00:00.000000
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

revision: str = "a4d7e2c91b83"
down_revision: Union[str, None] = "6f1a2b3c4d5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FKS = (
    ("organizations_parent_org_id_fkey", "parent_org_id"),
    ("organizations_root_org_id_fkey", "root_org_id"),
)


def upgrade() -> None:
    for name, column in _FKS:
        op.drop_constraint(name, "organizations", type_="foreignkey")
        op.create_foreign_key(
            name,
            "organizations",
            "organizations",
            [column],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    for name, column in _FKS:
        op.drop_constraint(name, "organizations", type_="foreignkey")
        op.create_foreign_key(
            name,
            "organizations",
            "organizations",
            [column],
            ["id"],
            ondelete="SET NULL",
        )
