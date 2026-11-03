"""analysis_results.smart_diagnosis: create the column the model already maps

Revision ID: 0053
Revises: 0052
Create Date: 2026-11-03 00:00:00.000000

The model maps analysis_results.smart_diagnosis (JSONB, nullable, no default) and live
has it, but no earlier migration creates it, so a database built from the migrations
lacked it. Same type, nullability and default as live. `IF NOT EXISTS` makes this a
no-op on live.

Downgrade is a no-op: on live the column predates this migration and holds data, and
afterwards the migration cannot tell a column it added from one live already had.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0053"
down_revision: str | Sequence[str] | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column if it is missing."""
    op.execute("ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS smart_diagnosis JSONB")


def downgrade() -> None:
    """No-op; see the docstring."""
