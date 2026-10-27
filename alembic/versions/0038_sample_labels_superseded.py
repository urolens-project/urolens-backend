"""sample_labels superseded flag

Revision ID: 0038
Revises: 0037
Create Date: 2026-09-27 00:00:00.000000

Renumbered from 0035 to 0038 while merging fix/UROLENS-142-Sample-Assignment
into development: this branch's own 0035 (queue_assignments_active_unique)
and two other, already-merged branches' migrations
(0035_lab_requests_test_type_and_special_instructions from
UROLENS-137/feat/lab-request, and 0036/0037_patients_add_sex/dedup_hash from
feat/patient-intake) all independently claimed revision IDs starting from
'0035' with down_revision '0034' — the same class of collision hit and
fixed once already during the feat/lab-request -> development merge (see
that merge commit). This branch was cut before that renumbering existed, so
it collided again. Chained after the current chain tip (0037) instead.

Adds `sample_labels.superseded` (UROLENS-141): `generateLabel` regenerates a
label by inserting a new `sample_labels` row rather than updating the old
one, so a specimen with more than one label had no way to tell which row is
current — `confirmLabelAffixed`'s `.first()` (no explicit ordering) could
grab a stale one. This column lets `generateLabel` mark every prior label
for a specimen `superseded=true` before inserting the new one, and lets
`confirmLabelAffixed`/the new print-job endpoint filter to the current label
explicitly instead of relying on insertion order.

NOT RUN AGAINST THE LIVE DATABASE — this environment has no network access
to the real DB (same constraint as 0032/0033/0034). Uses the same
ADD COLUMN IF NOT EXISTS idempotent-guard idiom as those migrations. The
migration chain is already known broken from an empty database (missing
`users`/`specimens`/`audit_logs` CREATE TABLE migrations — see 0032's docstring
and the `supabase-alembic-migration` skill) — this migration doesn't fix or
worsen that; it only adds a column to a table 0032 already formalized.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0038'
down_revision: str | Sequence[str] | None = '0037'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE sample_labels ADD COLUMN IF NOT EXISTS superseded "
        "BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE sample_labels DROP COLUMN IF EXISTS superseded")
