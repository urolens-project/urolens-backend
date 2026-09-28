"""result_releases one release per result

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-28 00:00:00.000000

Renumbered from 0040 to 0043. origin/development's tip is 0041
(0040_enable_rls_all_tables.py, 0041_make_microscopy_bucket_private.py, both
UROLENS-220) — chaining after 0041 alone would give this migration
revision '0042', but the sibling branch feat/UROLENS-169 already claims
'0042' for its own migration (0042_supervisor_stats_indexes.py, pushed to
origin/feat/UROLENS-169) off that same development tip. Chaining after that
instead avoids a second guaranteed collision, at the cost of an implicit
ordering dependency: whichever of feat/UROLENS-169 / feat/UROLENS-143 merges
into development second will need its migration's down_revision re-checked
against development's actual tip at that time — the same renumbering this
file itself just went through. Content unchanged from the original 0040
version.

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
revision: str = '0043'
down_revision: str | Sequence[str] | None = '0042'
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
