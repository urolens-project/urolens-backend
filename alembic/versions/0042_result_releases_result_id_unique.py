"""result_releases one release per result

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-28 00:00:00.000000

Renumbered from 0040 to 0042 (via a brief detour through 0043 — see below).
origin/development's real tip, now merged into this branch, is 0041
(0040_enable_rls_all_tables.py, 0041_make_microscopy_bucket_private.py, both
UROLENS-220). Chained after it directly. Content unchanged from the
original 0040 version.

This migration was briefly renumbered to 0043 (down_revision='0042') to
avoid colliding with the sibling branch feat/UROLENS-169's own migration,
which also claims '0042' off the same development tip (pushed to
origin/feat/UROLENS-169, not merged into development yet). That version
broke `alembic heads` on THIS branch standalone — down_revision='0042'
pointed at a revision that only exists on feat/UROLENS-169's tree, not
here, so CI (which runs on every push, testing the raw branch) failed with
a dangling-reference KeyError. Reverted to chaining directly after 0041
(this branch's own real, present tip, post-merge) instead of trying to
pre-empt a collision with a revision this branch doesn't actually contain.

Known consequence, not fixed here: this branch and feat/UROLENS-169 both
now claim '0042' off development's 0041. Whichever of the two merges into
development second will hit the same collision UROLENS-220 vs. this
migration originally hit, and will need its own migration renumbered
again at that time — a real coordination point between the two branches,
not something resolvable by one branch unilaterally reserving a number
for a revision it doesn't contain.

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
revision: str = '0042'
down_revision: str | Sequence[str] | None = '0041'
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
