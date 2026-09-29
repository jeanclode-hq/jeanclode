"""llm credential model aliases

Claude credentials now store a family alias (``haiku``/``sonnet``/``opus``/
``fable``) instead of a pinned model id, so the Agent SDK's bundled CLI picks
the newest model of that family and a model launch is a dependency bump, not
a data change. Rewrites the pinned ids saved by the old dropdown.

Revision ID: 9e3b7c5a2d14
Revises: 8d4e2a7c1f90
Create Date: 2026-09-29 12:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9e3b7c5a2d14"
down_revision: Union[str, Sequence[str], None] = "8d4e2a7c1f90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FAMILY = r"^claude-(haiku|sonnet|opus|fable)-"


def upgrade() -> None:
    for column in ("model_high", "model_heavy", "model_low"):
        op.execute(
            f"UPDATE llm_credentials "
            f"SET {column} = substring({column} from '{_FAMILY}') "
            f"WHERE provider IN ('claude_code', 'anthropic') AND {column} ~ '{_FAMILY}'"
        )


def downgrade() -> None:
    # The pinned ids aren't recoverable, and an alias stays valid for the SDK.
    pass
