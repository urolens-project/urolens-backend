"""queue_assignments one active assignment per specimen

Revision ID: 0039
Revises: 0038
Create Date: 2026-09-28 00:00:00.000000

Renumbered from 0035 to 0039 while merging this branch into development:
this branch's own 0035 (this file) collided with revision IDs already
claimed and renumbered by other, already-merged branches (see
0038_sample_labels_superseded.py's docstring for the same class of
collision, hit twice in this same merge). Chained after the current chain
tip (0038) instead.

UROLENS-142: `assignSpecimen` used to check-for-existing-assignment then
insert as two separate, unguarded steps — two concurrent requests for the
same specimen could both pass the check before either inserted, assigning
the same specimen to two MedTechs. A partial unique index enforces "at most
one ACTIVE assignment per specimen" at the database level regardless of how
many concurrent requests reach the insert; `queue_service.assignSpecimen`
catches the resulting `IntegrityError` (inside a SAVEPOINT via
`db.begin_nested()`) and reports the same `SPECIMEN_ALREADY_ASSIGNED` the
pre-check already raises in the non-race case.

NOT RUN AGAINST THE LIVE DATABASE — this environment has no network access
to the real DB (same constraint as every migration in this chain so far:
0032/0033/0034/0035-on-the-sibling-UROLENS-141-branch). `queue_assignments`
itself already has a real, applied migration (0010) unlike several of this
chain's out-of-band-created tables, so this one is a plain `CREATE INDEX
CONCURRENTLY`-shaped addition to a table that's already properly tracked —
still flagging per the standing constraint that the chain can't build from
an empty database today (missing `users`/`specimens`/`audit_logs` CREATE
TABLE migrations), which this migration doesn't fix or worsen.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0039'
down_revision: str | Sequence[str] | None = '0038'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "ix_queue_assignments_one_active_per_specimen "
        "ON queue_assignments (specimen_id) WHERE status = 'ACTIVE'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_queue_assignments_one_active_per_specimen")
