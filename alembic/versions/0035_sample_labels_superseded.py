"""sample_labels superseded flag

Revision ID: 0035
Revises: 0034
Create Date: 2026-09-27 00:00:00.000000

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
revision: str = '0035'
down_revision: str | Sequence[str] | None = '0034'
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
