"""patients add sex

Revision ID: 0036
Revises: 0035
Create Date: 2026-09-23 00:00:00.000000

Renumbered from 0035 to 0036 while merging feat/lab-request into
development: both branches independently created a migration numbered
0035 (this one, and 0035_lab_requests_test_type_and_special_instructions.py
from feat/lab-request/UROLENS-137) with the same down_revision (0034) — a
literal duplicate revision ID, which alembic can't disambiguate at all
(unlike two different IDs branching from the same parent, which would at
least be a legible multi-head error). Chained after the lab-requests
migration instead; 0037_patients_dedup_hash.py's down_revision was updated
to match.

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
revision: str = '0036'
down_revision: str | Sequence[str] | None = '0035'
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
