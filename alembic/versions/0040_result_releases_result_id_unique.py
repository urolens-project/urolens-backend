"""result_releases one release per result

Revision ID: 0040
Revises: 0039
Create Date: 2026-09-28 00:00:00.000000

UROLENS-143: `releaseResult` used to check-for-existing-release then insert
as two separate, unguarded steps — two concurrent requests for the same
result could both pass the check before either inserted. Unlike
`queue_assignments` (which allows multiple historical rows and only needs
"at most one ACTIVE"), a result release has no active/superseded distinction
at all — a result is released at most once, ever — so this is a plain unique
constraint on `result_id`, not a partial index.

NOT RUN AGAINST THE LIVE DATABASE — this environment has no network access
to the real DB (same constraint as every migration in this chain).
`result_releases` itself already has a real, applied migration (0026) with
only a plain (non-unique) index on `result_id`; this adds the missing
uniqueness on top of the existing, already-tracked column.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0040'
down_revision: str | Sequence[str] | None = '0039'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "ALTER TABLE result_releases ADD CONSTRAINT uq_result_releases_result_id "
        "UNIQUE (result_id)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "ALTER TABLE result_releases DROP CONSTRAINT IF EXISTS uq_result_releases_result_id"
    )
