"""patients add sex

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-23 00:00:00.000000

Same situation as migrations 0032/0033: `src/models/patient.py` declares
`sex = Column(VARCHAR(10), nullable=False)` and `PatientCreateRequest.sex`
is required, but no migration anywhere in this repo (nor the stray root
`migration_sql.sql`) ever creates a `sex` column on `patients`. Either it
was added directly to the live Supabase DB out of band (same pattern as
0032/0033) and only needs retrofitting into Alembic history, or it is
genuinely missing and every patient registration would fail against a
fresh schema.

`ADD COLUMN IF NOT EXISTS` makes this a safe no-op either way. A default
is required because the column is NOT NULL and existing rows (if any, in
the out-of-band-already-live case) need a value; 'OTHER' is used as a
neutral placeholder for pre-existing rows only — no application code
relies on this default, every new row always supplies an explicit sex.

Whoever applies this against the real database should diff it against the
actual live schema first, per the same caveat as 0032/0033.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0035'
down_revision: str | Sequence[str] | None = '0034'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE patients ADD COLUMN IF NOT EXISTS sex VARCHAR(10) NOT NULL DEFAULT 'OTHER'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE patients DROP COLUMN IF EXISTS sex")
