"""allow several credentials per subject

A skill or MCP server can now hold one credential per target host, so the
one-per-subject unique constraint becomes a plain lookup index.

Revision ID: 67e90faaeb78
Revises: c4d5e6f7a8b9
Create Date: 2026-09-25 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "67e90faaeb78"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace the one-credential-per-subject constraint with an index."""
    op.drop_constraint("uq_credential_subject", "credentials", type_="unique")
    op.create_index("ix_credentials_subject", "credentials", ["subject_type", "subject_id"])


def downgrade() -> None:
    """Keep each subject's most recent credential and restore the constraint."""
    op.execute(
        """
        DELETE FROM credentials c
        USING credentials newer
        WHERE newer.subject_type = c.subject_type
          AND newer.subject_id = c.subject_id
          AND (newer.updated_at, newer.id) > (c.updated_at, c.id)
        """
    )
    op.drop_index("ix_credentials_subject", table_name="credentials")
    op.create_unique_constraint(
        "uq_credential_subject", "credentials", ["subject_type", "subject_id"]
    )
