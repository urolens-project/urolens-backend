"""patients middle name clinical history

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-21 20:34:06.065024

Same situation as migration 0032: `middle_name` and `clinical_history` were
already live columns on `patients` — read/written by
src/urolens/services/patient_service.py and referenced in the dead stub at
src/urolens/domains/intake/models.py — but never modeled in SQLAlchemy.
`ADD COLUMN IF NOT EXISTS` is used for the same reason as 0032: this
environment cannot confirm the live schema (no network access to the real
database from the authoring sandbox), so this is a safe no-op if the columns
already exist rather than a migration that could fail against them. Whoever
applies this against the real database should diff it against the actual
live schema first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0033'
down_revision: Union[str, Sequence[str], None] = '0032'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS middle_name TEXT")
    op.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS clinical_history TEXT")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE patients DROP COLUMN IF EXISTS clinical_history")
    op.execute("ALTER TABLE patients DROP COLUMN IF EXISTS middle_name")
